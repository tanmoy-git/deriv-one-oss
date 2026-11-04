"""The agent map: agents -> skills -> systems / secrets / channels as one graph.

Answers two admin questions at a glance:

* "What can touch Salesforce?" -- focus on the system and walk upstream to the
  skills linked to it and the agents those skills are attached to.
* "What breaks if I rotate this token?" -- focus on the secret and walk
  upstream the same way.

Everything here is a pure function over rows the router has already loaded, so
it can be tested without a database. Secrets appear by name only: callers pass
skill secret *names* (``SkillSecret.env_key``) and, for agents, whether each
credential slot is filled. No vault reference or value reaches this module.

Node ids are ``<type>:<key>``. System keys are lower-cased names because the
catalogue matches names case-insensitively; skill secrets are keyed by env var
name, so two skills that both declare ``SALESFORCE_TOKEN`` share one node --
the usual meaning when the same upstream credential is configured twice.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any, Iterable, Mapping

NODE_TYPES = ("agent", "skill", "system", "secret", "channel")

# Agent credential columns and the labels shown for them. Only presence is
# read (``bool(ref)``) -- the ref itself is never copied into the graph.
AGENT_CREDENTIAL_SLOTS: tuple[tuple[str, str], ...] = (
    ("xoxb_secret_ref", "Slack bot token"),
    ("xapp_secret_ref", "Slack app token"),
    ("signing_secret_ref", "Slack signing secret"),
    ("client_secret_ref", "Slack client secret"),
    ("gateway_api_key_ref", "Model gateway key"),
)


def _loads_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        value = raw
    else:
        try:
            value = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v not in (None, "")]


def system_node_id(name: str) -> str:
    return f"system:{name.strip().lower()}"


def normalize_focus(focus: str) -> str:
    """Accept ``system:Salesforce`` as well as the canonical lower-case id."""
    focus = focus.strip()
    kind, _, key = focus.partition(":")
    if kind == "system":
        return system_node_id(key)
    return focus


class _Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: dict[str, dict] = {}

    def node(self, node_id: str, type_: str, label: str, **fields: Any) -> None:
        if node_id in self.nodes:
            return
        self.nodes[node_id] = {
            "id": node_id,
            "type": type_,
            "label": label,
            "status": fields.get("status"),
            "risk_level": fields.get("risk_level"),
            "detail": fields.get("detail"),
        }

    def edge(self, source: str, target: str, kind: str, direction: str | None = None) -> None:
        edge_id = f"{source}->{target}"
        existing = self.edges.get(edge_id)
        if existing is None:
            self.edges[edge_id] = {
                "id": edge_id,
                "source": source,
                "target": target,
                "kind": kind,
                "direction": direction,
            }
        elif direction and existing["direction"] != direction:
            # A skill can be linked to one system through several tags, each with
            # its own direction; the edge carries the widest of them.
            existing["direction"] = "read_write" if existing["direction"] else direction


def build_agent_map(
    *,
    agents: Iterable[Any],
    channel_configs: Iterable[Any] = (),
    system_links: Iterable[Any] = (),
    skill_secret_names: Iterable[tuple[str, str]] = (),
    skill_names: Mapping[str, str] | None = None,
    skill_risk: Mapping[str, str | None] | None = None,
    catalog: Mapping[str, Mapping[str, Any]] | None = None,
    focus: str | None = None,
) -> dict:
    """Build the graph. Returns ``{"nodes", "edges", "focus", "totals"}``.

    * ``agents``: Agent-like rows (decommissioned ones are skipped).
    * ``channel_configs``: AgentChannelConfig-like rows; supply channel names
      and per-channel skill overrides.
    * ``system_links``: SkillSystemLink-like rows.
    * ``skill_secret_names``: ``(skill_slug, env_key)`` pairs.
    * ``skill_names`` / ``skill_risk``: display name and risk level by slug,
      from the skills ACL manifest. A slug is "known" if it appears here.
    * ``catalog``: ``system_catalog.get_system_map()`` output.
    * ``focus``: a node id; when set, only that node, everything upstream of it
      and everything downstream of it are returned.

    Raises ``KeyError`` if ``focus`` names a node that is not in the graph.
    """
    skill_names = skill_names or {}
    skill_risk = skill_risk or {}
    catalog = catalog or {}
    g = _Graph()

    def add_skill(slug: str) -> str:
        node_id = f"skill:{slug}"
        g.node(
            node_id, "skill", skill_names.get(slug) or slug,
            risk_level=skill_risk.get(slug),
            detail=None if slug in skill_names else "Not in the skills manifest",
        )
        return node_id

    live_agents = [a for a in agents if getattr(a, "status", None) != "decommissioned"]
    live_ids = {a.id for a in live_agents}

    configs_by_agent: dict[str, list[Any]] = defaultdict(list)
    for cfg in channel_configs:
        if cfg.agent_id in live_ids and getattr(cfg, "is_active", True):
            configs_by_agent[cfg.agent_id].append(cfg)

    for agent in live_agents:
        agent_id = f"agent:{agent.id}"
        g.node(agent_id, "agent", agent.name or agent.id,
               status=agent.status, detail=getattr(agent, "agent_type", None))

        for slug in _loads_list(agent.skill_ids):
            g.edge(agent_id, add_skill(slug), "uses_skill")

        channel_names: dict[str, str | None] = {cid: None for cid in _loads_list(agent.channel_ids)}
        for cfg in configs_by_agent.get(agent.id, []):
            channel_names[cfg.channel_id] = getattr(cfg, "channel_name", None) or channel_names.get(cfg.channel_id)
            # Channel overrides attach skills the agent may not carry globally.
            for slug in _loads_list(getattr(cfg, "skill_ids", None)):
                g.edge(agent_id, add_skill(slug), "uses_skill")
        for cid, cname in channel_names.items():
            node_id = f"channel:{cid}"
            g.node(node_id, "channel", f"#{cname}" if cname else cid, detail=cid if cname else None)
            # A name may only be known from another agent's config row.
            if cname and g.nodes[node_id]["label"] == cid:
                g.nodes[node_id].update(label=f"#{cname}", detail=cid)
            g.edge(agent_id, node_id, "in_channel")

        for column, label in AGENT_CREDENTIAL_SLOTS:
            if getattr(agent, column, None):
                node_id = f"secret:agent:{agent.id}:{column.removesuffix('_secret_ref').removesuffix('_ref')}"
                g.node(node_id, "secret", label, detail=f"Credential of {agent.name or agent.id}")
                g.edge(agent_id, node_id, "holds_credential")

    # Skills that exist only in links or secrets are shown when they are known
    # to the manifest or already attached to an agent; anything else is a
    # leftover row for an unpublished skill.
    def skill_in_scope(slug: str) -> bool:
        return slug in skill_names or f"skill:{slug}" in g.nodes

    for link in system_links:
        if not link.system_name or not skill_in_scope(link.skill_slug):
            continue
        sys_id = system_node_id(link.system_name)
        record = catalog.get(link.system_name.strip().lower())
        g.node(
            sys_id, "system",
            (record or {}).get("system_name") or link.system_name,
            risk_level=(record or {}).get("risk_level") or link.risk_level,
            status=(record or {}).get("system_status") if record else None,
            detail=None if record or not catalog else "Not in the active system catalogue",
        )
        g.edge(add_skill(link.skill_slug), sys_id, "touches_system", link.direction)

    for slug, env_key in skill_secret_names:
        if not env_key or not skill_in_scope(slug):
            continue
        node_id = f"secret:{env_key}"
        g.node(node_id, "secret", env_key, detail="Skill secret")
        g.edge(add_skill(slug), node_id, "needs_secret")

    nodes, edges = g.nodes, g.edges
    focus_id = None
    if focus:
        focus_id = normalize_focus(focus)
        if focus_id not in nodes:
            raise KeyError(focus_id)
        keep = connected_ids(focus_id, edges.values())
        nodes = {k: v for k, v in nodes.items() if k in keep}
        edges = {k: v for k, v in edges.items() if v["source"] in keep and v["target"] in keep}

    ordered_nodes = sorted(nodes.values(), key=lambda n: (NODE_TYPES.index(n["type"]), n["label"].lower(), n["id"]))
    totals = {t: 0 for t in NODE_TYPES}
    for n in ordered_nodes:
        totals[n["type"]] += 1
    return {
        "nodes": ordered_nodes,
        "edges": sorted(edges.values(), key=lambda e: e["id"]),
        "focus": focus_id,
        "totals": totals,
    }


def connected_ids(focus_id: str, edges: Iterable[Mapping[str, Any]]) -> set[str]:
    """The focus node plus everything strictly upstream and downstream of it.

    Walking the two directions separately (rather than the undirected
    component) keeps "what touches Salesforce" from pulling in every other
    system those agents also reach.
    """
    downstream: dict[str, list[str]] = defaultdict(list)
    upstream: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        downstream[e["source"]].append(e["target"])
        upstream[e["target"]].append(e["source"])

    keep = {focus_id}
    for adjacency in (downstream, upstream):
        seen = {focus_id}
        queue = deque([focus_id])
        while queue:
            for nxt in adjacency.get(queue.popleft(), []):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        keep |= seen
    return keep
