import { useEffect, useState } from "react";
import { fetchHealth, type HealthResponse } from "../services/api";

export default function Dashboard() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchHealth()
      .then(setHealth)
      .catch(() => setError("Unable to reach backend API"));
  }, []);

  const statusColor = (status: string) => (status === "healthy" ? "text-green-600" : "text-red-500");

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold text-gray-800">Dashboard</h2>
        <p className="text-gray-500 mt-1">System overview — Phase 1 Foundation</p>
      </div>
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 max-w-md">
        <h3 className="font-semibold text-gray-700 mb-4">Backend Connectivity</h3>
        {error && <p className="text-red-500 text-sm">{error}</p>}
        {health && (
          <ul className="space-y-2 text-sm">
            <li className="flex justify-between">
              <span>API</span>
              <span className={statusColor(health.services.api)}>{health.services.api}</span>
            </li>
            <li className="flex justify-between">
              <span>PostgreSQL</span>
              <span className={statusColor(health.services.postgres)}>{health.services.postgres}</span>
            </li>
            <li className="flex justify-between">
              <span>Redis</span>
              <span className={statusColor(health.services.redis)}>{health.services.redis}</span>
            </li>
          </ul>
        )}
        {!health && !error && <p className="text-gray-400 text-sm">Checking system status...</p>}
      </div>
    </div>
  );
}
