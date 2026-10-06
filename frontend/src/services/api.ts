// Thin wrappers over the shared axios instance in ../api/client.
// This module previously hardcoded http://localhost:5000/make-call, which no
// backend ever served, and did not export the fetchHealth/HealthResponse that
// pages/Dashboard.tsx imports.
import { apiClient, initiateOutboundCall, type OutboundCallResponse } from "../api/client";

export interface HealthResponse {
  status: "healthy" | "degraded";
  services: {
    api: string;
    postgres: string;
    redis: string;
  };
}

export async function fetchHealth(): Promise<HealthResponse> {
  const { data } = await apiClient.get<HealthResponse>("/api/v1/health");
  return data;
}

export async function initiateCall(phoneNumber: string): Promise<OutboundCallResponse> {
  return initiateOutboundCall(phoneNumber);
}
