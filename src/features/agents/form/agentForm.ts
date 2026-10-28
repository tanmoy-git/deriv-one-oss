import { AgentFormState } from "@/components/AgentFormFields";
import { AgentRecord } from "@/data/agents";

export function fromAgent(a: AgentRecord): AgentFormState {
  return {
    name: a.name,
    description: a.description ?? "",
    usecase_description: a.usecase_description ?? "",
    avatar_url: a.avatar_url,
    slack_app_id: a.slack_app_id,
    skill_ids: [...(a.skill_ids ?? [])],
    channel_ids: [...(a.channel_ids ?? [])],
    trigger_mode: a.trigger_mode,
    soul_text: a.soul_text ?? "",
    llm_model: a.llm_model ?? null,
    schedule_enabled: false,
    schedule_cron: "0 9 * * *",
    schedule_prompt: "",
    schedule_output_channel: "",
  };
}
