import { BrowserRouter } from "react-router-dom";
import { AuthProvider } from "./auth/AuthContext";
import AppRoutes from "./router";

/**
 * Application root.
 *
 * `AuthProvider` sits above the router so every guard, the sidebar and the
 * tenant switcher all read one source of truth for role and tenant.
 */
export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </AuthProvider>
  );
}
