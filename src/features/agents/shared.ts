

export interface ScheduledTask {
  id: string;
  agent_id: string;
  prompt: string;
  schedule: string;
  timezone: string;
  output_channel: string;
  is_active: boolean;
  report_enabled: boolean;
  report_prompt: string | null;
  default_report_prompt: string;
  last_run_at: string | null;
  last_run_status: string | null;
  run_count: number;
  created_at: string;
}
