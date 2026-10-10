import BookingEscalations from "../components/BookingEscalations";
import { Route, Routes } from "react-router-dom";
import AppShell from "../layouts/AppShell";
import AnalyticsView from "../pages/AnalyticsView";
import CalendarView from "../pages/CalendarView";
import CommandCenter from "../pages/CommandCenter";
import Login from "../pages/Login";
import NotFound from "../pages/NotFound";
import LeadsPage from "../pages/sales/LeadsPage";
import OutboundCampaigns from "../pages/sales/OutboundCampaigns";
import SpaCalls from "../pages/spa/SpaCalls";
import SpaGuests from "../pages/spa/SpaGuests";
import SpaSettings from "../pages/spa/SpaSettings";
import SpaServices from "../pages/spa/SpaServices";
import AskCara from "../pages/spa/AskCara";
import { RequireAuth, RequireRole } from "./guards";
import { ROUTE_ROLES } from "./navigation";

/**
 * Route table.
 *
 * Each protected route wraps its element in `RequireRole` with the roles taken
 * from `ROUTE_ROLES` — the same table the sidebar builds from. A route and its
 * nav item therefore cannot disagree about who may see it, which is the failure
 * mode that leaves a "hidden" page reachable by typing its URL.
 */
export default function AppRoutes() {
  const guard = (path: string, element: React.ReactNode) => (
    <RequireRole roles={ROUTE_ROLES[path]}>{element}</RequireRole>
  );

  return (
    <Routes>
      <Route path="/login" element={<Login />} />

      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route index element={guard("/", <CommandCenter />)} />

        {/* 6DM Sales Agent — super_admin only. */}
        <Route path="sales/leads" element={guard("/sales/leads", <LeadsPage />)} />
        <Route
          path="sales/campaigns"
          element={guard("/sales/campaigns", <OutboundCampaigns />)}
        />
        <Route
          path="sales/analytics"
          element={guard(
            "/sales/analytics",
            <AnalyticsView eyebrow="6DM Sales Agent" title="Sales analytics" />
          )}
        />
        <Route
          path="sales/calendar"
          element={guard(
            "/sales/calendar",
            <CalendarView
              eyebrow="6DM Sales Agent"
              title="Dominic's calendar"
              subtitle="Sales presentations booked by the outbound agent."
            />
          )}
        />

        {/* Spa Receptionist — scoped to the caller's own tenant. */}
        <Route path="spa/calls" element={guard("/spa/calls", <SpaCalls />)} />
        <Route path="spa/attention" element={guard("/spa/attention", <div className="p-6"><BookingEscalations /></div>)} />
        <Route path="spa/cara" element={guard("/spa/cara", <AskCara />)} />
        <Route path="spa/guests" element={guard("/spa/guests", <SpaGuests />)} />
        <Route path="spa/services" element={guard("/spa/services", <SpaServices />)} />
        <Route path="spa/settings" element={guard("/spa/settings", <SpaSettings />)} />
        <Route
          path="spa/calendar"
          element={
            <CalendarView
              eyebrow="Spa Receptionist"
              title="Service calendar"
              subtitle="Appointments booked by your AI receptionist."
            />
          }
        />

        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
