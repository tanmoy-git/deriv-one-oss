import { type ReactNode, useState, useEffect, useCallback, useRef, lazy, Suspense } from "react";
import { BrowserRouter, Routes, Route, NavLink, Navigate, useLocation, useParams } from "react-router-dom";
import { MessageSquare, Home, Terminal, Zap, Brain, Bot, Sparkles, Settings, ShieldCheck, PanelLeftClose, PanelLeftOpen, Wand2, FlaskConical, Workflow } from "lucide-react";
import { SkillAccessProvider, useSkillAccess } from "@/lib/SkillAccessContext";
import { WorkspaceChatProvider, useWorkspaceStreams } from "@/src/lib/workspaceChat";
import { useBranding } from "@/lib/branding";
import { TourProvider } from "@/src/lib/tour/TourProvider";
import { getInitials } from "@/lib/user";
import ScrollToTop from "./components/ScrollToTop";
import FeedbackProvider, { useFeedback } from "./feedback/FeedbackProvider";
import AnnotateToggle from "./feedback/AnnotateToggle";

const LandingPage = lazy(() => import("./pages/LandingPage"));
const SkillsPage = lazy(() => import("./pages/SkillsPage"));
const SkillDetailPage = lazy(() => import("./pages/SkillDetailPage"));
const PublishPage = lazy(() => import("./pages/PublishPage"));
const CreateSkillPage = lazy(() => import("./pages/CreateSkillPage"));
const DocumentationPage = lazy(() => import("./pages/DocumentationPage"));
const IntegrationsPage = lazy(() => import("./pages/IntegrationsPage"));
const MemoryPage = lazy(() => import("./pages/MemoryPage"));
const AutomationsPage = lazy(() => import("./pages/AutomationsPage"));
const AgentsPage = lazy(() => import("./pages/AgentsPage"));
const AgentRegisterPage = lazy(() => import("./pages/AgentRegisterPage"));
const AgentDetailPage = lazy(() => import("./features/agents/AgentDetailPage"));
const AgentOAuthPendingPage = lazy(() => import("./pages/AgentOAuthPendingPage"));
const AdminPage = lazy(() => import("./pages/AdminPage"));
const WorkspacePage = lazy(() => import("./pages/WorkspacePage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));
const AppsPage = lazy(() => import("./pages/AppsPage"));
const LabPage = lazy(() => import("./pages/LabPage"));
const PlatformAgentPage = lazy(() => import("./pages/PlatformAgentPage"));
const SetupWizardPage = lazy(() => import("./pages/SetupWizardPage"));
const FlowsPage = lazy(() => import("./pages/FlowsPage"));

const NAV_ITEMS = [
  { to: "/", icon: Home, label: "Home", exact: true },
  { to: "/workspace", icon: MessageSquare, label: "Workspace" },
  { to: "/skills", icon: Terminal, label: "Skills", tourId: "nav-skills" },
  { to: "/integrations", icon: Zap, label: "Integrations", tourId: "nav-integrations" },
  { to: "/memory", icon: Brain, label: "Memory", tourId: "nav-memory" },
  { to: "/agents", icon: Sparkles, label: "Agents", tourId: "nav-agents" },
  { to: "/automations", icon: Bot, label: "Automations", tourId: "nav-automations" },
  { to: "/flows", icon: Workflow, label: "Flows" },
  { to: "/lab", icon: FlaskConical, label: "Lab", beta: true },
];

function IconSidebar() {
  const location = useLocation();
  const { employee } = useSkillAccess();
  const { isAdmin } = useFeedback();
  const branding = useBranding();
  const activeStreams = useWorkspaceStreams();
  const workspaceWorking = activeStreams.length > 0;
  const [expanded, setExpanded] = useState<boolean>(() => {
    try { return localStorage.getItem("sidebar-expanded") !== "false"; } catch { return true; }
  });

  useEffect(() => {
    try { localStorage.setItem("sidebar-expanded", String(expanded)); } catch { /* ignore */ }
  }, [expanded]);

  const displayName = employee?.name ?? employee?.email ?? "My Account";
  const displayEmail = employee?.email ?? "";
  const initials = employee ? getInitials(employee.name, employee.email) : "??";

  const navItemClass = (isActive: boolean) =>
    `flex items-center gap-3 rounded-lg px-2.5 py-2 transition-colors relative group ${
      isActive
        ? "bg-[var(--brand)] text-white shadow-sm"
        : "text-slate-500 hover:bg-[var(--brand-soft)] hover:text-[var(--brand)]"
    }`;

  const tooltip = (label: string) => (
    !expanded && (
      <span className="absolute left-full ml-3 px-2.5 py-1 bg-[#0E0E0E] text-white text-xs font-medium rounded-md whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none z-50 shadow-lg">
        {label}
      </span>
    )
  );

  return (
    <div
      data-deriv-section="sidebar-nav"
      className={`shrink-0 bg-white border-r border-slate-200 flex flex-col py-3 z-10 transition-all duration-200 ${expanded ? "w-52" : "w-14 items-center"}`}
    >
      {/* Logo + brand */}
      <NavLink
        to="/"
        end
        className={`flex items-center gap-2.5 mb-4 px-2.5 rounded-lg hover:bg-[var(--brand-soft)] transition-colors ${!expanded && "justify-center px-0"}`}
        title="Home"
      >
        <div className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0 overflow-hidden">
          <img src={branding.app_logo_url} alt={branding.app_short_name} className="w-8 h-8 object-contain" />
        </div>
        {expanded && (
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 leading-none">
              <span className="text-sm font-bold text-[#0E0E0E] truncate">{branding.app_short_name}</span>
            </div>
            <div className="text-[10px] text-emerald-600 font-mono mt-0.5 truncate">● Connected</div>
          </div>
        )}
      </NavLink>

      {/* Collapse toggle */}
      <button
        onClick={() => setExpanded(v => !v)}
        className={`flex items-center gap-3 rounded-lg px-2.5 py-2 text-slate-400 hover:bg-[var(--brand-soft)] hover:text-[var(--brand)] transition-colors mb-2 ${!expanded && "justify-center w-9 px-0 mx-auto"}`}
        title={expanded ? "Collapse sidebar" : "Expand sidebar"}
      >
        {expanded
          ? <><PanelLeftClose className="w-4 h-4 shrink-0" /><span className="text-sm font-medium">Collapse</span></>
          : <PanelLeftOpen className="w-4 h-4 shrink-0" />
        }
      </button>

      <div className={`w-full border-t border-slate-100 mb-2 ${!expanded && "mx-auto w-8"}`} />

      {/* Main nav */}
      <nav className={`flex flex-col gap-0.5 flex-1 ${expanded ? "px-2" : "items-center px-0 gap-1"}`}>
        {NAV_ITEMS.map(({ to, icon: Icon, label, exact, beta, tourId }) => {
          const showWorking = to === "/workspace" && workspaceWorking;
          return (
            <NavLink
              key={to}
              to={to}
              end={exact}
              data-tour={tourId}
              className={({ isActive }) => navItemClass(isActive) + (!expanded ? " w-9 justify-center" : "")}
            >
              <Icon className="w-4 h-4 shrink-0" />
              {showWorking && (
                <span className="absolute top-1.5 right-1.5 w-2 h-2 rounded-full bg-amber-400 animate-pulse" title="Workspace active" />
              )}
              {expanded && <span className="text-sm font-medium truncate">{label}</span>}
              {expanded && beta && (
                <span className="ml-auto text-[9px] font-bold uppercase tracking-wider bg-[var(--brand)]/10 text-[var(--brand)] px-1.5 py-0.5 rounded-full">
                  Beta
                </span>
              )}
              {tooltip(label)}
            </NavLink>
          );
        })}
      </nav>

      <div className={`border-t border-slate-100 my-2 ${expanded ? "mx-2" : "w-8 mx-auto"}`} />

      {/* Bottom actions */}
      <div className={`flex flex-col gap-0.5 ${expanded ? "px-2" : "items-center"}`}>
        {isAdmin && (
          <NavLink
            to="/admin"
            end
            className={({ isActive }) => navItemClass(isActive) + (!expanded ? " w-9 justify-center" : "")}
          >
            <ShieldCheck className="w-4 h-4 shrink-0" />
            {expanded && <span className="text-sm font-medium truncate">Admin</span>}
            {tooltip("Admin")}
          </NavLink>
        )}
        {isAdmin && (
          <NavLink
            to="/admin/platform-agent"
            className={({ isActive }) => navItemClass(isActive) + (!expanded ? " w-9 justify-center" : "")}
          >
            <Sparkles className="w-4 h-4 shrink-0" />
            {expanded && <span className="text-sm font-medium truncate">Platform Agent</span>}
            {tooltip("Platform Agent")}
          </NavLink>
        )}

        <NavLink
          to="/settings"
          className={({ isActive }) => navItemClass(isActive) + (!expanded ? " w-9 justify-center" : "")}
        >
          <Settings className="w-4 h-4 shrink-0" />
          {expanded && <span className="text-sm font-medium truncate">Settings</span>}
          {tooltip("Settings")}
        </NavLink>
      </div>

      {/* User avatar */}
      <div className={`mt-3 ${expanded ? "px-2" : "flex justify-center"}`}>
        <NavLink
          to="/settings"
          className={`flex items-center gap-2.5 rounded-lg p-2 hover:bg-slate-100 transition-colors ${!expanded && "w-9 justify-center"}`}
          title="Settings"
        >
          <div className="w-7 h-7 rounded-full bg-[#FF444F]/10 border border-[#FF444F]/30 flex items-center justify-center text-xs font-bold text-[#FF444F] shrink-0">
            {initials}
          </div>
          {expanded && (
            <div className="min-w-0">
              <div className="text-xs font-semibold text-slate-700 truncate" title={displayName}>{displayName}</div>
              <div className="text-[10px] text-slate-400 truncate" title={displayEmail}>{displayEmail}</div>
            </div>
          )}
        </NavLink>
      </div>
    </div>
  );
}

function PageWrapper({ children, fullHeight = false }: { children: ReactNode; fullHeight?: boolean }) {
  return (
    <div className={`flex min-w-0 w-full flex-1 flex-col overflow-hidden ${fullHeight ? "h-full min-h-0" : ""}`}>
      {children}
    </div>
  );
}

function RouteFallback() {
  return (
    <div className="flex-1 flex items-center justify-center py-32 text-slate-400">
      <div className="w-6 h-6 border-2 border-slate-300 border-t-slate-600 rounded-full animate-spin" />
    </div>
  );
}

function RedirectOvernightToLab() {
  const { id } = useParams<{ id: string }>();
  return <Navigate to={id ? `/lab/${id}` : "/apps"} replace />;
}

/** Send admins to the setup wizard until first-run configuration is complete. */
function HomePageHeader() {
  const branding = useBranding();
  return (
    <div className="flex items-center justify-between px-6 py-3 bg-white border-b border-slate-200 shrink-0">
      <div className="flex items-center gap-2">
        <img src={branding.app_logo_url} alt="" className="w-6 h-6" />
        <span className="text-sm font-bold text-[#0E0E0E]">{branding.app_short_name}</span>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-[10px] font-mono text-emerald-600 uppercase tracking-wider">● Connected</span>
      </div>
    </div>
  );
}

function SetupAdminGate({ children }: { children: ReactNode }) {
  const location = useLocation();
  const branding = useBranding();
  const { isAdmin } = useFeedback();
  if (
    isAdmin &&
    branding.setup_complete === false &&
    !location.pathname.startsWith("/admin/setup")
  ) {
    return <Navigate to="/admin/setup" replace />;
  }
  return <>{children}</>;
}

export default function App() {
  const automationsCreateTriggerRef = useRef<(() => void) | null>(null);
  const onRequestCreate = useCallback((trigger: () => void) => {
    automationsCreateTriggerRef.current = trigger;
  }, []);

  return (
    <BrowserRouter>
      <ScrollToTop />
      <FeedbackProvider>
      <SkillAccessProvider>
      <WorkspaceChatProvider>
      <TourProvider>
      <SetupAdminGate>
      <div className="h-screen flex overflow-hidden font-sans bg-[#F2F3F4] text-[#0E0E0E]">
        <IconSidebar />
        <main data-deriv-section="main-content" className="flex-1 flex flex-col overflow-hidden">
          <Suspense fallback={<RouteFallback />}>
          <Routes>
            <Route path="/" element={
              <PageWrapper>
                <HomePageHeader />
                <div className="flex-1 overflow-y-auto">
                  <LandingPage />
                </div>
              </PageWrapper>
            } />

            <Route path="/skills" element={
              <PageWrapper>
                <div className="flex items-center justify-between px-6 py-3 bg-white border-b border-slate-200 shrink-0">
                  <span className="text-sm font-semibold text-slate-700">Skills <span className="text-slate-400 font-normal">— Registry</span></span>
                  <div className="flex items-center gap-2">
                    <NavLink to="/create-skill" className="flex items-center gap-1.5 text-xs font-bold border border-slate-200 text-slate-700 px-3 py-1.5 rounded-md hover:bg-slate-50 transition-colors">
                      <Wand2 className="w-3.5 h-3.5" />
                      Create Skill
                    </NavLink>
                    <NavLink to="/publish" className="text-xs font-bold bg-[#FF444F] hover:bg-[#e63d48] text-white px-3 py-1.5 rounded-md transition-colors">
                      Publish Skill
                    </NavLink>
                  </div>
                </div>
                <div className="flex-1 overflow-y-auto">
                  <SkillsPage />
                </div>
              </PageWrapper>
            } />

            <Route path="/skills/:slug" element={
              <PageWrapper>
                <div className="flex-1 overflow-y-auto">
                  <SkillDetailPage />
                </div>
              </PageWrapper>
            } />

            <Route path="/integrations" element={
              <PageWrapper fullHeight>
                <div className="px-6 py-3 bg-white border-b border-slate-200 shrink-0">
                  <span className="text-sm font-semibold text-slate-700">Integrations <span className="text-slate-400 font-normal">— Connected Tools</span></span>
                </div>
                <div className="flex-1 flex overflow-hidden">
                  <IntegrationsPage />
                </div>
              </PageWrapper>
            } />

            <Route path="/memory" element={
              <PageWrapper fullHeight>
                <div className="px-6 py-3 bg-white border-b border-slate-200 shrink-0">
                  <span className="text-sm font-semibold text-slate-700">Memory <span className="text-slate-400 font-normal">— Context & Knowledge</span></span>
                </div>
                <div className="flex-1 flex overflow-hidden">
                  <MemoryPage />
                </div>
              </PageWrapper>
            } />

            <Route path="/agents" element={
              <PageWrapper fullHeight>
                <div className="px-6 py-3 bg-white border-b border-slate-200 shrink-0">
                  <span className="text-sm font-semibold text-slate-700">Agents <span className="text-slate-400 font-normal">— Slack Bots You Own</span></span>
                </div>
                <div className="flex-1 flex overflow-hidden">
                  <AgentsPage />
                </div>
              </PageWrapper>
            } />

            <Route path="/agents/new" element={
              <PageWrapper fullHeight>
                <div className="px-6 py-3 bg-white border-b border-slate-200 shrink-0">
                  <span className="text-sm font-semibold text-slate-700">Agents <span className="text-slate-400 font-normal">— Register New</span></span>
                </div>
                <div className="flex-1 overflow-hidden">
                  <AgentRegisterPage />
                </div>
              </PageWrapper>
            } />

            <Route path="/agents/oauth-pending" element={
              <PageWrapper fullHeight>
                <div className="flex-1 overflow-hidden">
                  <AgentOAuthPendingPage />
                </div>
              </PageWrapper>
            } />

            <Route path="/admin/setup" element={
              <PageWrapper fullHeight>
                <div className="flex-1 overflow-hidden">
                  <SetupWizardPage />
                </div>
              </PageWrapper>
            } />

            <Route path="/admin" element={
              <PageWrapper fullHeight>
                <div className="flex-1 overflow-hidden">
                  <AdminPage />
                </div>
              </PageWrapper>
            } />

            {/* Legacy redirects */}
            <Route path="/admin/access"    element={<Navigate to="/admin" replace />} />
            <Route path="/admin/directory" element={<Navigate to="/admin" replace />} />

            <Route path="/admin/platform-agent" element={
              <PageWrapper fullHeight>
                <div className="flex-1 overflow-hidden">
                  <PlatformAgentPage />
                </div>
              </PageWrapper>
            } />
            <Route path="/admin/deriv-one-agent" element={<Navigate to="/admin/platform-agent" replace />} />

            <Route path="/blueprints/*" element={<Navigate to="/" replace />} />

            <Route path="/agents/:id" element={
              <PageWrapper fullHeight>
                <div className="flex-1 overflow-hidden">
                  <AgentDetailPage />
                </div>
              </PageWrapper>
            } />

            <Route path="/automations" element={
              <PageWrapper fullHeight>
                <div className="px-6 py-3 bg-white border-b border-slate-200 shrink-0 flex items-center justify-between">
                  <span className="text-sm font-semibold text-slate-700">Automations <span className="text-slate-400 font-normal">— Agents & Schedules</span></span>
                  <button
                    onClick={() => automationsCreateTriggerRef.current?.()}
                    className="text-xs font-bold bg-[#FF444F] hover:bg-[#e63d48] text-white px-3 py-1.5 rounded-md transition-colors">
                    + New Automation
                  </button>
                </div>
                <div className="flex-1 flex overflow-hidden">
                  <AutomationsPage onRequestCreate={onRequestCreate} />
                </div>
              </PageWrapper>
            } />

            <Route path="/workspace" element={
              <PageWrapper fullHeight>
                <div className="flex-1 flex overflow-hidden">
                  <WorkspacePage />
                </div>
              </PageWrapper>
            } />

            <Route path="/flows" element={
              <PageWrapper fullHeight>
                <div className="px-6 py-3 bg-white border-b border-slate-200 shrink-0">
                  <span className="text-sm font-semibold text-slate-700">Flows <span className="text-slate-400 font-normal">— Skill chains</span></span>
                </div>
                <div className="flex min-h-0 w-full flex-1 overflow-hidden">
                  <FlowsPage />
                </div>
              </PageWrapper>
            } />
            <Route path="/computer" element={<Navigate to="/flows" replace />} />

            <Route path="/docs" element={
              <PageWrapper>
                <div className="px-6 py-3 bg-white border-b border-slate-200 shrink-0">
                  <span className="text-sm font-semibold text-slate-700">Documentation</span>
                </div>
                <div className="flex-1 overflow-y-auto">
                  <DocumentationPage />
                </div>
              </PageWrapper>
            } />

            <Route path="/publish" element={
              <PageWrapper>
                <div className="flex-1 overflow-y-auto">
                  <PublishPage />
                </div>
              </PageWrapper>
            } />

            <Route path="/create-skill" element={
              <PageWrapper fullHeight>
                <div className="px-6 py-3 bg-white border-b border-slate-200 shrink-0 flex items-center justify-between">
                  <span className="text-sm font-semibold text-slate-700">Skills <span className="text-slate-400 font-normal">— AI Creator</span></span>
                  <NavLink to="/publish" className="text-xs font-semibold text-slate-500 hover:text-slate-900 transition-colors">
                    Manual publish →
                  </NavLink>
                </div>
                <div className="flex-1 flex flex-col overflow-hidden">
                  <CreateSkillPage />
                </div>
              </PageWrapper>
            } />

            <Route path="/lab" element={
              <PageWrapper fullHeight>
                <div className="px-6 py-3 bg-white border-b border-slate-200 shrink-0 flex items-center justify-between">
                  <span className="text-sm font-semibold text-slate-700 flex items-center gap-2">
                    Lab <span className="text-slate-400 font-normal">— AI App Builder</span>
                    <span className="text-[9px] font-bold uppercase tracking-wider bg-[#FF444F]/10 text-[#FF444F] px-1.5 py-0.5 rounded-full">Beta</span>
                  </span>
                  <NavLink
                    to="/lab"
                    state={{ openNew: true }}
                    className="text-xs font-bold bg-[#FF444F] hover:bg-[#e63d48] text-white px-3 py-1.5 rounded-md transition-colors"
                  >
                    + New App
                  </NavLink>
                </div>
                <div className="flex-1 flex w-full overflow-hidden">
                  <AppsPage />
                </div>
              </PageWrapper>
            } />

            <Route path="/lab/:id" element={
              <PageWrapper fullHeight>
                <div className="flex-1 flex overflow-hidden">
                  <LabPage />
                </div>
              </PageWrapper>
            } />

            <Route path="/lab/:id/overnight" element={<RedirectOvernightToLab />} />
            <Route path="/migrate" element={<Navigate to="/" replace />} />

            <Route path="/settings" element={
              <PageWrapper fullHeight>
                <div className="px-6 py-3 bg-white border-b border-slate-200 shrink-0">
                  <span className="text-sm font-semibold text-slate-700">Settings</span>
                </div>
                <div className="flex-1 flex overflow-hidden">
                  <SettingsPage />
                </div>
              </PageWrapper>
            } />
          </Routes>
          </Suspense>
        </main>
      </div>
      <AnnotateToggle />
      </SetupAdminGate>
      </TourProvider>
      </WorkspaceChatProvider>
      </SkillAccessProvider>
      </FeedbackProvider>
    </BrowserRouter>
  );
}
