import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export interface PlatformConfig {
  app_name: string;
  app_short_name: string;
  app_logo_url: string;
  brand_color: string;
  brand_color_hover: string;
  brand_color_soft: string;
  org_noun: string;
  slack_auto_create_enabled: boolean;
  slack_config_collaborator: string;
  setup_complete?: boolean;
  directory_provider?: string;
}

const DEFAULT_CONFIG: PlatformConfig = {
  app_name: "Platform",
  app_short_name: "Platform",
  app_logo_url: "/app-logo-icon.svg",
  brand_color: "#FF444F",
  brand_color_hover: "#e03d47",
  brand_color_soft: "#FFF0F0",
  org_noun: "employee",
  slack_auto_create_enabled: false,
  slack_config_collaborator: "",
  // Unknown until /api/platform/config answers. SetupAdminGate only redirects
  // on an explicit false, so leaving this unset stops it from sending admins to
  // the setup wizard on every page load before the config arrives.
  setup_complete: undefined,
};

export interface BrandingContextValue extends PlatformConfig {
  refresh: () => Promise<void>;
}

const BrandingContext = createContext<BrandingContextValue>({
  ...DEFAULT_CONFIG,
  refresh: async () => {},
});

function applyBrandCss(config: PlatformConfig) {
  const root = document.documentElement;
  root.style.setProperty("--brand", config.brand_color);
  root.style.setProperty("--brand-hover", config.brand_color_hover);
  root.style.setProperty("--brand-soft", config.brand_color_soft);
}

export function BrandingProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<PlatformConfig>(DEFAULT_CONFIG);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/platform/config");
      if (!res.ok) return;
      const data = (await res.json()) as PlatformConfig;
      const merged = { ...DEFAULT_CONFIG, ...data };
      setConfig(merged);
      applyBrandCss(merged);
    } catch {
      applyBrandCss(DEFAULT_CONFIG);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const value = useMemo<BrandingContextValue>(
    () => ({ ...config, refresh }),
    [config, refresh],
  );
  return <BrandingContext.Provider value={value}>{children}</BrandingContext.Provider>;
}

export function useBranding(): BrandingContextValue {
  return useContext(BrandingContext);
}

/** Replace {{APP_NAME}} placeholders in copy strings. */
export function withAppName(text: string, appName: string): string {
  return text.replaceAll("{{APP_NAME}}", appName);
}
