import axios, { type AxiosInstance } from "axios";

type ApiErrorDetail = string | { msg?: string; loc?: Array<string | number> }[];

/** Convert FastAPI string or validation-list errors into safe UI text. */
export const getApiErrorMessage = (error: unknown, fallback: string): string => {
  if (!axios.isAxiosError(error)) return fallback;

  const detail = error.response?.data?.detail as ApiErrorDetail | undefined;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === "string") return item;
        const location = item.loc?.filter((part) => part !== "body").join(" > ");
        return location ? `${location}: ${item.msg || "Invalid value"}` : item.msg || "Invalid value";
      })
      .filter(Boolean);
    if (messages.length) return messages.join(" ");
  }
  return fallback;
};

const BASE_URL: string =
  (import.meta.env.NEXT_PUBLIC_API_URL as string) ||
  (import.meta.env.VITE_API_BASE_URL as string) ||
  "http://127.0.0.1:8000";

export const ACCESS_TOKEN_KEY = "access_token";
export const REFRESH_TOKEN_KEY = "refresh_token";
const ACTIVE_TENANT_KEY = "active_tenant_id";

export const apiClient: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  timeout: 10000,
  headers: { "Content-Type": "application/json" },
});

// ---------------------------------------------------------------------------
// Tenant impersonation (super admin tenant switcher)
// ---------------------------------------------------------------------------
// Held here rather than threaded through every call site so no request can
// accidentally omit it. The backend ignores this header for spa users and
// rejects it outright if it names a tenant they don't belong to, so it is a
// convenience for 6DM staff, never a way to widen access.
let activeTenantId: string | null = localStorage.getItem(ACTIVE_TENANT_KEY);

export const getActiveTenantId = (): string | null => activeTenantId;

export const setActiveTenantId = (tenantId: string | null): void => {
  activeTenantId = tenantId;
  if (tenantId) localStorage.setItem(ACTIVE_TENANT_KEY, tenantId);
  else localStorage.removeItem(ACTIVE_TENANT_KEY);
};

apiClient.interceptors.request.use((config) => {
  const token = localStorage.getItem(ACCESS_TOKEN_KEY) || localStorage.getItem("token");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  if (activeTenantId) {
    config.headers["X-Tenant-Id"] = activeTenantId;
  }
  return config;
});

/** Fires when the API rejects our credentials, so AuthContext can sign out. */
export type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler | null = null;

export const setUnauthorizedHandler = (handler: UnauthorizedHandler | null): void => {
  onUnauthorized = handler;
};

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      onUnauthorized?.();
    }
    return Promise.reject(error);
  }
);

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------
export type UserRole = "super_admin" | "spa_admin" | "spa_staff";

export interface CurrentUser {
  id: string;
  email: string;
  full_name: string | null;
  is_active: boolean;
  is_business_master?: boolean;
  role: UserRole;
  tenant_id: string | null;
  twilio_phone_number: string | null;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

export type BookingProvider =
  | "google_calendar"
  | "mindbody"
  | "mangomint"
  | "square"
  | "vagaro"
  | "zenoti";

export interface BusinessHoursWindow {
  open: string;
  close: string;
}

export interface SpaService {
  resource_ids?: string[];
  preparation_buffer_minutes?: number;
  transition_buffer_minutes?: number;
  cleanup_buffer_minutes?: number;
  square_variation_id?: string | null;
  square_variation_version?: number | null;
  name: string;
  duration_minutes: number;
  price?: string | null;
  description?: string | null;
}

export interface ServiceCategory {
  id: string;
  name: string;
  description: string | null;
  is_active: boolean;
  service_count: number;
  created_at: string;
  updated_at: string;
}

export interface ManagedService {
  id: string;
  category_id: string;
  category_name: string;
  name: string;
  description: string | null;
  price: string | null;
  duration_minutes: number;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface SpaStaffMember {
  provider_id?: string;
  hours?: Record<string, BusinessHoursWindow[]>;
  special_hours?: Record<string, BusinessHoursWindow[]>;
  name: string;
  role?: string | null;
  services: string[];
}

export interface EnhancementSettings {
  enabled: boolean;
  max_suggestions: number;
  personalize: boolean;
  retention_days: number;
  excluded_services: string[];
  rules: { base_service: string; target_service: string; priority: number; requires_resources: boolean; phrase_variants?: string[] }[];
}
export interface EnhancementReport {
  eligible: number; presented: number; accepted: number; declined: number; booked: number;
  acceptance_rate: number | null;
  incremental_booking_value_minor: Record<string, number>;
  realized_revenue: null;
  by_service: { base_service: string; enhancement: string | null; presented: number; accepted: number; declined: number; booked: number }[];
}
export const fetchEnhancementReport = async (id: string): Promise<EnhancementReport> =>
  (await apiClient.get(`/api/v1/spa-accounts/${id}/enhancements/report`)).data;
export const clearEnhancementHistory = async (id: string): Promise<void> => {
  await apiClient.delete(`/api/v1/spa-accounts/${id}/enhancements/history`);
};
export const generateEnhancementPhrasing = async (id: string): Promise<string[]> =>
  (await apiClient.post(`/api/v1/spa-accounts/${id}/enhancements/phrasing`)).data.phrases;

export interface SpaAccount {
  booking_policies?: { visit?: {
    enabled?: boolean; allow_reorder?: boolean; allow_after_hours?: boolean; max_services?: number;
    special_hours?: Record<string, BusinessHoursWindow[]>; after_hours?: Record<string, BusinessHoursWindow[]>;
  }; [key: string]: unknown };
  id: string;
  name: string;
  location: string | null;
  twilio_phone_number: string | null;
  grok_system_prompt: string | null;
  business_hours: Record<string, BusinessHoursWindow[]>;
  services: SpaService[];
  staff: SpaStaffMember[];
  timezone: string;
  booking_provider: BookingProvider;
  booking_config: Record<string, string>;
  twiml_voice: string | null;
  is_active: boolean;
  booking_provider_configured: boolean;
  created_at: string;
  updated_at: string;
  description: string | null;
  public_phone: string | null;
  cancellation_policy: string | null;
  amenities: string[];
  packages: unknown[];
  enhancement_settings?: EnhancementSettings;
  upsell_rules: { base_service: string; allowed_upsells: string[] }[];
  payment_policy: {
    card_required: boolean;
    collection_mode: "none" | "at_spa" | "square_link" | "secure_sms_link";
    script?: string | null;
  };
}

export interface GoogleCalendarStatus {
  status: string;
  connected: boolean;
  google_account_email: string | null;
  selected_calendar_id: string | null;
  last_tested_at: string | null;
}

export interface GoogleCalendarOption {
  id: string;
  summary: string;
  description?: string | null;
  timeZone?: string | null;
  accessRole?: string | null;
  primary?: boolean;
}

export interface SpaAccountSummary {
  id: string;
  name: string;
  twilio_phone_number: string | null;
  booking_provider: BookingProvider;
  is_active: boolean;
}

export interface Contact {
  id: string;
  owner_id: string | null;
  tenant_id: string | null;
  first_name?: string | null;
  last_name?: string | null;
  full_name: string;
  phone_number: string;
  email?: string | null;
  extra_metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export type ContactInput = {
  first_name?: string | null;
  last_name?: string | null;
  phone_number: string;
  email?: string | null;
};

export interface Lead extends Contact {
  call_count: number;
  last_call_at: string | null;
  upcoming_appointment_at: string | null;
}

// Mirrors backend CallStatus / CallDirection (app/models/call_log.py) — these are
// snake_case enum *values*, not hyphenated.
export type CallStatus =
  | "queued"
  | "ringing"
  | "in_progress"
  | "completed"
  | "busy"
  | "failed"
  | "no_answer"
  | "cancelled";

export type CallDirection = "inbound" | "outbound";

export interface CallLog {
  id: string;
  user_id: string | null;
  tenant_id: string | null;
  contact_id: string | null;
  contact?: { id: string; full_name: string; phone_number: string } | null;
  twilio_call_sid: string;
  direction: CallDirection;
  status: CallStatus;
  from_number: string;
  to_number: string;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  transcript: string | null;
  recording_url: string | null;
  ai_summary: string | null;
  primary_language: string | null;
  ai_analysis: Record<string, unknown>;
  created_at: string;
}

export type AppointmentStatus =
  | "scheduled"
  | "confirmed"
  | "cancelled"
  | "completed"
  | "no_show";

export type CardStatus =
  | "not_required"
  | "not_supported"
  | "unknown"
  | "pending_card"
  | "card_confirmed"
  | "failed";

export interface Appointment {
  business_timezone?: string;
  id: string;
  user_id: string | null;
  tenant_id: string | null;
  contact_id: string;
  source_call_id: string | null;
  title: string;
  description: string | null;
  status: AppointmentStatus;
  card_status?: CardStatus;
  start_time: string;
  end_time: string;
  booking_provider: string | null;
  external_booking_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface AnalyticsSummary {
  tenant_id: string | null;
  scope: "spa" | "sales_workspace";
  window_start: string;
  window_end: string;
  calls: {
    total: number;
    inbound: number;
    outbound: number;
    completed: number;
    failed: number;
  };
  bookings: {
    total: number;
    scheduled: number;
    confirmed: number;
    cancelled: number;
    completed: number;
    no_show: number;
  };
  sentiment: {
    positive: number;
    neutral: number;
    negative: number;
    unscored: number;
  };
  average_call_duration_seconds: number | null;
  booking_conversion_rate: number;
  contacts_total: number;
}

// Backend list endpoints return the paginated envelope from
// app/schemas/common.py (Page[T]), not a bare array.
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
}

/** The number on the other end of the call, whichever direction it went. */
export const counterpartyNumber = (log: CallLog): string =>
  log.direction === "outbound" ? log.to_number : log.from_number;

export interface OutboundCallResponse {
  call_sid: string;
  call_log_id: string;
  status: string;
}

// NOTE: no trailing slashes below. The routers register their list/create routes
// as `@router.get("")`, so "/api/v1/contacts/" triggers a 307 redirect — an extra
// round trip that also forces a second CORS preflight in the browser.

// --- Auth ---
export const login = async (email: string, password: string): Promise<TokenPair> => {
  const { data } = await apiClient.post<TokenPair>("/api/v1/auth/login", { email, password });
  return data;
};

export const logout = async (refreshToken: string): Promise<void> => {
  await apiClient.post("/api/v1/auth/logout", { refresh_token: refreshToken });
};

export const fetchMe = async (): Promise<CurrentUser> => {
  const { data } = await apiClient.get<CurrentUser>("/api/v1/auth/me");
  return data;
};

// --- Spa accounts ---
export const fetchSpaAccounts = async (): Promise<SpaAccountSummary[]> => {
  const { data } = await apiClient.get<Page<SpaAccountSummary>>("/api/v1/spa-accounts");
  return data.items;
};

export interface SpaAccountCreateInput {
  name: string;
  twilio_phone_number?: string;
  timezone: string;
  business_hours: Record<string, BusinessHoursWindow[]>;
  services: SpaService[];
  staff: SpaStaffMember[];
  booking_provider: BookingProvider;
  booking_config: Record<string, unknown>;
}

export const createSpaAccount = async (payload: SpaAccountCreateInput): Promise<SpaAccount> => {
  const { data } = await apiClient.post<SpaAccount>("/api/v1/spa-accounts", payload);
  return data;
};

export const registerUser = async (payload: {
  email: string;
  password: string;
  full_name: string;
  role: UserRole;
  tenant_id: string;
}): Promise<CurrentUser> => {
  const { data } = await apiClient.post<CurrentUser>("/api/v1/auth/register", payload);
  return data;
};

export const fetchMySpaAccount = async (): Promise<SpaAccount> => {
  const { data } = await apiClient.get<SpaAccount>("/api/v1/spa-accounts/me");
  return data;
};

export const fetchSpaAccount = async (spaId: string): Promise<SpaAccount> => {
  const { data } = await apiClient.get<SpaAccount>(`/api/v1/spa-accounts/${spaId}`);
  return data;
};

export const updateSpaAccount = async (
  spaId: string,
  patch: Partial<
    Pick<
      SpaAccount,
      | "name"
      | "location"
      | "grok_system_prompt"
      | "business_hours"
      | "services"
      | "staff"
      | "timezone"
      | "booking_provider"
      | "booking_config"
      | "twiml_voice"
      | "description"
      | "public_phone"
      | "cancellation_policy"
      | "amenities"
      | "packages"
      | "booking_policies"
      | "enhancement_settings"
      | "upsell_rules"
      | "payment_policy"
    >
  >
): Promise<SpaAccount> => {
  const { data } = await apiClient.patch<SpaAccount>(`/api/v1/spa-accounts/${spaId}`, patch);
  return data;
};

export const fetchServiceCategories = async (): Promise<ServiceCategory[]> => {
  const { data } = await apiClient.get<ServiceCategory[]>('/api/v1/services/categories');
  return data;
};

export const createServiceCategory = async (payload: { name: string; description?: string }): Promise<ServiceCategory> => {
  const { data } = await apiClient.post<ServiceCategory>('/api/v1/services/categories', payload);
  return data;
};

export const updateServiceCategory = async (id: string, payload: { name: string; description?: string; is_active?: boolean }): Promise<ServiceCategory> => {
  const { data } = await apiClient.patch<ServiceCategory>(`/api/v1/services/categories/${id}`, payload);
  return data;
};

export const deleteServiceCategory = async (id: string): Promise<void> => {
  await apiClient.delete(`/api/v1/services/categories/${id}`);
};

export const fetchManagedServices = async (categoryId?: string): Promise<ManagedService[]> => {
  const { data } = await apiClient.get<ManagedService[]>('/api/v1/services', {
    params: categoryId ? { category_id: categoryId } : undefined,
  });
  return data;
};

export type ServicePayload = Omit<ManagedService, 'id' | 'category_name' | 'created_at' | 'updated_at'>;

export const createManagedService = async (payload: ServicePayload): Promise<ManagedService> => {
  const { data } = await apiClient.post<ManagedService>('/api/v1/services', payload);
  return data;
};

export const updateManagedService = async (id: string, payload: ServicePayload): Promise<ManagedService> => {
  const { data } = await apiClient.patch<ManagedService>(`/api/v1/services/${id}`, payload);
  return data;
};

export const deleteManagedService = async (id: string): Promise<void> => {
  await apiClient.delete(`/api/v1/services/${id}`);
};

export const bulkDeleteManagedServices = async (payload: {
  service_ids?: string[];
  delete_all?: boolean;
}): Promise<{ deleted: number }> => {
  const { data } = await apiClient.post<{ deleted: number }>(
    "/api/v1/services/bulk-delete",
    payload,
  );
  return data;
};

export type ProviderHealth = {
  twilio: "connected" | "disconnected" | "not_configured";
  grok: "connected" | "disconnected" | "not_configured";
};

export const fetchProviderHealth = async (): Promise<ProviderHealth> => {
  const { data } = await apiClient.get<ProviderHealth>("/api/v1/health/providers");
  return data;
};

export type BookingConnectionStatus =
  | "connected"
  | "not_configured"
  | "authorization_required"
  | "calendar_not_selected"
  | "calendar_not_found"
  | "insufficient_permissions"
  | "token_expired_or_revoked"
  | "google_api_error"
  | "needs_attention"
  | "connection_failed";

export const testSpaBookingConnection = async (spaId: string): Promise<{ status: BookingConnectionStatus; missing: string[] }> => {
  const { data } = await apiClient.post<{ status: BookingConnectionStatus; missing: string[] }>(
    `/api/v1/spa-accounts/${spaId}/booking/test`
  );
  return data;
};

export const connectGoogleCalendar = async (spaId: string): Promise<{ authorization_url: string }> => {
  const { data } = await apiClient.get<{ authorization_url: string }>(
    `/api/v1/spa-accounts/${spaId}/booking/google/connect`
  );
  return data;
};

export const fetchGoogleCalendarStatus = async (spaId: string): Promise<GoogleCalendarStatus> => {
  const { data } = await apiClient.get<GoogleCalendarStatus>(
    `/api/v1/spa-accounts/${spaId}/booking/google/status`
  );
  return data;
};

export const fetchGoogleCalendars = async (spaId: string): Promise<{ status: string; calendars: GoogleCalendarOption[] }> => {
  const { data } = await apiClient.get<{ status: string; calendars: GoogleCalendarOption[] }>(
    `/api/v1/spa-accounts/${spaId}/booking/google/calendars`
  );
  return data;
};

export const selectGoogleCalendar = async (spaId: string, calendarId: string): Promise<void> => {
  await apiClient.put(`/api/v1/spa-accounts/${spaId}/booking/google/calendar`, { calendar_id: calendarId });
};

export const disconnectGoogleCalendar = async (spaId: string): Promise<void> => {
  await apiClient.post(`/api/v1/spa-accounts/${spaId}/booking/google/disconnect`);
};

// --- Contacts ---
export const fetchContacts = async (): Promise<Contact[]> => {
  const { data } = await apiClient.get<Page<Contact>>("/api/v1/contacts");
  return Array.isArray(data?.items) ? data.items : [];
};

export const createContact = async (contact: ContactInput): Promise<Contact> => {
  const { data } = await apiClient.post<Contact>("/api/v1/contacts", contact);
  return data;
};

// --- Leads (6DM Sales Agent · super_admin only) ---
export const fetchLeads = async (params?: {
  search?: string;
  booked?: boolean;
}): Promise<Page<Lead>> => {
  const { data } = await apiClient.get<Page<Lead>>("/api/v1/leads", { params });
  return data;
};

export const createLead = async (lead: ContactInput): Promise<Lead> => {
  const { data } = await apiClient.post<Lead>("/api/v1/leads", lead);
  return data;
};

// --- Calls ---
export const fetchCallLogs = async (params?: {
  direction?: CallDirection;
}): Promise<CallLog[]> => {
  const { data } = await apiClient.get<Page<CallLog>>("/api/v1/calls", { params });
  return data.items;
};

// --- Appointments ---
export const fetchAppointments = async (params?: {
  from_time?: string;
  to_time?: string;
}): Promise<Appointment[]> => {
  const { data } = await apiClient.get<Page<Appointment>>("/api/v1/appointments", { params });
  return data.items;
};

// --- Analytics ---
export const fetchAnalytics = async (): Promise<AnalyticsSummary> => {
  const { data } = await apiClient.get<AnalyticsSummary>("/api/v1/analytics");
  return data;
};

// --- Outbound calls (6DM Sales Agent · super_admin only) ---
export const initiateOutboundCall = async (
  targetPhone: string,
  callObjective?: string
): Promise<OutboundCallResponse> => {
  const { data } = await apiClient.post<OutboundCallResponse>(
    "/api/v1/telephony/voice/outbound",
    {
      to_number: targetPhone,
      call_objective: callObjective || "General appointment scheduling",
    }
  );
  return data;
};
