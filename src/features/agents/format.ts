import { AgentInteractionRecord } from "@/data/agents";

export function formatInteractionWhen(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function interactionTriggerLabel(trigger: AgentInteractionRecord["trigger_type"]): string {
  switch (trigger) {
    case "dm":
      return "DM";
    case "a2a":
      return "Agent call";
    case "mention":
    default:
      return "Mention";
  }
}
