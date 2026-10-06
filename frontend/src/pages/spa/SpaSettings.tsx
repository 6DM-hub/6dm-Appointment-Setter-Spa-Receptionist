import { useEffect, useState } from "react";
import { Link, Lock, Phone, RefreshCw, Save, TestTube, Unlink } from "lucide-react";
import {
  fetchMySpaAccount,
  fetchSpaAccount,
  updateSpaAccount,
  type BookingProvider,
  type SpaAccount,
  testSpaBookingConnection,
  connectGoogleCalendar,
  disconnectGoogleCalendar,
  fetchGoogleCalendarStatus,
  fetchGoogleCalendars,
  selectGoogleCalendar,
  type GoogleCalendarOption,
  type GoogleCalendarStatus,
  getApiErrorMessage,
} from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { canEditSpaSettings } from "../../auth/roles";
import { PageHeader, Panel, StateBlock } from "../../components/ui/Primitives";


const PROVIDERS: BookingProvider[] = [
  "google_calendar",
  "mindbody",
  "mangomint",
  "square",
  "vagaro",
  "zenoti",
];

const PROVIDER_FIELDS: Record<BookingProvider, { key: string; label: string; secret?: boolean }[]> = {
  google_calendar: [],
  mindbody: [
    { key: "site_id", label: "Site ID" },
    { key: "api_key", label: "API key", secret: true },
    { key: "source_name", label: "Source name" },
    { key: "source_password", label: "Source password", secret: true },
  ],
  mangomint: [{ key: "api_key", label: "API key", secret: true }, { key: "location_id", label: "Location ID" }],
  square: [{ key: "access_token", label: "Access token", secret: true }, { key: "location_id", label: "Location ID" }, { key: "application_id", label: "Application ID" }],
  vagaro: [{ key: "api_key", label: "API key", secret: true }, { key: "business_id", label: "Business ID" }],
  zenoti: [{ key: "api_key", label: "API key", secret: true }, { key: "center_id", label: "Center ID" }],
};

const DAYS: [string, string][] = [
  ["mon", "Monday"],
  ["tue", "Tuesday"],
  ["wed", "Wednesday"],
  ["thu", "Thursday"],
  ["fri", "Friday"],
  ["sat", "Saturday"],
  ["sun", "Sunday"],
];

/**
 * The receptionist configuration for one spa — the whole of what onboarding
 * touches. Everything here is a column on the tenant's `SpaAccount` row, read
 * by the inbound webhook at call time, so editing it changes how the spa's
 * phone is answered without a deploy.
 */
export default function SpaSettings() {
  const { role, effectiveTenantId, impersonatedTenantId } = useAuth();
  const editable = canEditSpaSettings(role);

  const [spa, setSpa] = useState<SpaAccount | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState<string | null>(null);
  const [googleStatus, setGoogleStatus] = useState<GoogleCalendarStatus | null>(null);
  const [googleCalendars, setGoogleCalendars] = useState<GoogleCalendarOption[]>([]);
  const [googleBusy, setGoogleBusy] = useState(false);

  useEffect(() => {
    setLoading(true);
    // A super admin reads the spa they picked in the switcher; a spa user's own
    // tenant comes from their token, with no id to supply or tamper with.
    const request = impersonatedTenantId
      ? fetchSpaAccount(impersonatedTenantId)
      : fetchMySpaAccount();
    request
      .then((account) => {
        setSpa(account);
        setError(null);
      })
      .catch((err: unknown) => setError(getApiErrorMessage(
        err,
        "No spa account is attached to this view. Pick one from the tenant switcher."
      )))
      .finally(() => setLoading(false));
  }, [impersonatedTenantId, effectiveTenantId]);

  useEffect(() => {
    if (!spa || spa.booking_provider !== "google_calendar") {
      setGoogleStatus(null);
      setGoogleCalendars([]);
      return;
    }
    fetchGoogleCalendarStatus(spa.id)
      .then((value) => {
        setGoogleStatus(value);
        if (value.connected) return fetchGoogleCalendars(spa.id);
        return null;
      })
      .then((value) => {
        if (value) setGoogleCalendars(value.calendars);
      })
      .catch(() => setGoogleStatus(null));
  }, [spa?.id, spa?.booking_provider]);

  const startGoogleConnect = async () => {
    if (!spa) return;
    setGoogleBusy(true);
    try {
      const { authorization_url } = await connectGoogleCalendar(spa.id);
      window.location.assign(authorization_url);
    } catch {
      setConnectionStatus("authorization required");
      setGoogleBusy(false);
    }
  };

  const refreshGoogleCalendars = async () => {
    if (!spa) return;
    setGoogleBusy(true);
    try {
      const value = await fetchGoogleCalendars(spa.id);
      setGoogleCalendars(value.calendars);
      setGoogleStatus((current) => current ? { ...current, status: value.status } : current);
    } finally {
      setGoogleBusy(false);
    }
  };

  const chooseGoogleCalendar = async (calendarId: string) => {
    if (!spa || !calendarId) return;
    setGoogleBusy(true);
    try {
      await selectGoogleCalendar(spa.id, calendarId);
      setGoogleStatus((current) => current ? { ...current, selected_calendar_id: calendarId, status: "connected" } : current);
      patch("booking_config", { ...spa.booking_config, google_calendar_id: calendarId });
    } finally {
      setGoogleBusy(false);
    }
  };

  const disconnectGoogle = async () => {
    if (!spa) return;
    setGoogleBusy(true);
    try {
      await disconnectGoogleCalendar(spa.id);
      setGoogleStatus({ status: "not_configured", connected: false, google_account_email: null, selected_calendar_id: null, last_tested_at: null });
      setGoogleCalendars([]);
      patch("booking_config", {});
      setConnectionStatus(null);
    } finally {
      setGoogleBusy(false);
    }
  };

  const patch = <K extends keyof SpaAccount>(key: K, value: SpaAccount[K]) => {
    setSpa((current) => (current ? { ...current, [key]: value } : current));
    setSaved(false);
  };

  const setHours = (day: string, field: "open" | "close", value: string) => {
    if (!spa) return;
    const existing = spa.business_hours[day]?.[0] ?? { open: "", close: "" };
    const next = { ...spa.business_hours };
    if (!value && field === "open") delete next[day];
    else next[day] = [{ ...existing, [field]: value }];
    patch("business_hours", next);
  };


  const save = async () => {
    if (!spa) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await updateSpaAccount(spa.id, {
        name: spa.name,
        location: spa.location ?? "",
        grok_system_prompt: spa.grok_system_prompt,
        business_hours: spa.business_hours,
        services: spa.services,
        staff: spa.staff,
        timezone: spa.timezone,
        booking_provider: spa.booking_provider,
        booking_config: spa.booking_config,
        twiml_voice: spa.twiml_voice,
        description: spa.description,
        public_phone: spa.public_phone,
        cancellation_policy: spa.cancellation_policy,
        amenities: spa.amenities ?? [],
        packages: spa.packages ?? [],
        upsell_rules: spa.upsell_rules ?? [],
        payment_policy: spa.payment_policy ?? { card_required: false, collection_mode: "none" },
      });
      setSpa(updated);
      setSaved(true);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Unable to save settings."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <PageHeader
        eyebrow="Spa Receptionist"
        title="Receptionist settings"
        subtitle="How your AI answers the phone: persona, service menu, team and opening hours."
        actions={
          editable && spa ? (
            <button
              onClick={save}
              disabled={saving}
              className="inline-flex items-center gap-2 rounded-xl bg-cyan-400 px-4 py-2.5 text-xs font-bold text-slate-950 transition hover:bg-cyan-300 disabled:opacity-60"
            >
              <Save size={15} /> {saving ? "Saving…" : "Save changes"}
            </button>
          ) : undefined
        }
      />

      {!editable && (
        <p className="flex items-center gap-2 rounded-xl border border-slate-700 bg-slate-800/40 px-3 py-2.5 text-[11px] text-slate-400">
          <Lock size={13} /> Your account has read-only access to these settings.
        </p>
      )}
      {saved && (
        <p className="rounded-xl border border-emerald-400/20 bg-emerald-400/5 px-3 py-2.5 text-[11px] text-emerald-300">
          Saved. New calls will use these settings immediately.
        </p>
      )}

      <StateBlock loading={loading} error={error}>
        {spa && (
          <div className="grid gap-6 lg:grid-cols-2">
            <Panel title="Identity">
              <label className="block">
                <span className="mb-2 block text-xs font-medium text-slate-400">
                  Spa name
                </span>
                <input
                  value={spa.name}
                  disabled={!editable}
                  onChange={(event) => patch("name", event.target.value)}
                  className="w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white outline-none disabled:opacity-60"
                />
              </label>

              <label className="block">
                <span className="mb-2 block text-xs font-medium text-slate-400">
                  Business location
                </span>

                <input
                  value={spa.location ?? ""}
                  disabled={!editable}
                  onChange={(event) => patch("location", event.target.value)}
                  placeholder="e.g. 123 Main Street, City, State"
                  className="w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white outline-none disabled:opacity-60"
                />
              </label>

              <label className="mt-4 block">
                <span className="mb-2 block text-xs font-medium text-slate-400">Public phone</span>
                <input
                  value={spa.public_phone ?? ""}
                  disabled={!editable}
                  onChange={(event) => patch("public_phone", event.target.value)}
                  placeholder="Shown to callers when they ask for your number"
                  className="w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white outline-none disabled:opacity-60"
                />
              </label>

              <label className="mt-4 block">
                <span className="mb-2 block text-xs font-medium text-slate-400">Business description</span>
                <textarea
                  rows={3}
                  value={spa.description ?? ""}
                  disabled={!editable}
                  onChange={(event) => patch("description", event.target.value)}
                  className="w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white outline-none disabled:opacity-60"
                />
              </label>

              <div className="mt-4">
                <span className="mb-2 block text-xs font-medium text-slate-400">
                  Inbound number
                </span>
                <div className="flex items-center gap-2 rounded-lg border border-slate-800 bg-[#07111f] px-3 py-2.5">
                  <Phone size={14} className="text-slate-500" />
                  <span className="text-sm text-slate-300">
                    {spa.twilio_phone_number || "not assigned"}
                  </span>
                  <Lock size={12} className="ml-auto text-slate-600" />
                </div>
                <p className="mt-1.5 text-[10px] text-slate-500">
                  Assigned by 6DM. Calls to this number are routed to your
                  receptionist.
                </p>
              </div>

              <label className="mt-4 block">
                <span className="mb-2 block text-xs font-medium text-slate-400">
                  Timezone
                </span>
                <input
                  value={spa.timezone}
                  disabled={!editable}
                  onChange={(event) => patch("timezone", event.target.value)}
                  placeholder="America/Los_Angeles"
                  className="w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white outline-none disabled:opacity-60"
                />
                <span className="mt-1.5 block text-[10px] text-slate-500">
                  Opening hours below are interpreted in this timezone.
                </span>
              </label>

              <label className="mt-4 block">
                <span className="mb-2 block text-xs font-medium text-slate-400">
                  Booking system
                </span>
                <select
                  value={spa.booking_provider}
                  disabled={!editable}
                  onChange={(event) => {
                    patch("booking_provider", event.target.value as BookingProvider);
                    patch("booking_config", {});
                    setConnectionStatus(null);
                  }}
                  className="w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-slate-200 outline-none disabled:opacity-60"
                >
                  {PROVIDERS.map((provider) => (
                    <option key={provider} value={provider}>
                      {provider.replace(/_/g, " ")}
                    </option>
                  ))}
                </select>
                {!spa.booking_provider_configured && (
                  <span className="mt-1.5 block text-[10px] text-amber-300">
                    This provider is not ready. Inbound bookings will be refused
                    until the required configuration is complete.
                  </span>
                )}
              </label>
            </Panel>

            <Panel title="Booking Integration" subtitle="Only this spa's provider configuration is used for inbound bookings.">
              <div className="space-y-3">
                {spa.booking_provider === "google_calendar" ? (
                  <div className="space-y-3">
                    {googleStatus?.connected ? (
                      <>
                        <div className="flex items-center justify-between gap-3 text-xs text-slate-300">
                          <span>Connected as <strong className="text-white">{googleStatus.google_account_email ?? "Google account"}</strong></span>
                          <span className="text-emerald-300">{googleStatus.status.replace(/_/g, " ")}</span>
                        </div>
                        <label className="block">
                          <span className="mb-1.5 block text-xs font-medium text-slate-400">Calendar</span>
                          <select
                            value={googleStatus.selected_calendar_id ?? ""}
                            disabled={!editable || googleBusy || googleCalendars.length === 0}
                            onChange={(event) => void chooseGoogleCalendar(event.target.value)}
                            className="w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white outline-none disabled:opacity-60"
                          >
                            <option value="">Select a calendar</option>
                            {googleCalendars.map((calendar) => (
                              <option key={calendar.id} value={calendar.id}>{calendar.summary}</option>
                            ))}
                          </select>
                        </label>
                      </>
                    ) : (
                      <div className="flex items-center justify-between gap-3 rounded-lg border border-dashed border-slate-700 px-3 py-3">
                        <span className="text-xs text-slate-400">Google Calendar is not connected.</span>
                        {editable && (
                          <button type="button" disabled={googleBusy} onClick={() => void startGoogleConnect()} className="inline-flex items-center gap-2 rounded-lg border border-cyan-400/40 px-3 py-2 text-[11px] font-semibold text-cyan-300 disabled:opacity-50">
                            <Link size={14} /> Connect Google Calendar
                          </button>
                        )}
                      </div>
                    )}
                    <div className="flex flex-wrap items-center gap-2 pt-1">
                      {googleStatus?.connected && editable && (
                        <>
                          <button type="button" disabled={googleBusy} onClick={() => void refreshGoogleCalendars()} title="Refresh accessible calendars" className="inline-flex items-center gap-2 rounded-lg border border-slate-700 px-3 py-2 text-[11px] font-semibold text-slate-300 disabled:opacity-50">
                            <RefreshCw size={14} /> Refresh calendars
                          </button>
                          <button type="button" disabled={googleBusy} onClick={() => void disconnectGoogle()} title="Disconnect Google Calendar" className="inline-flex items-center gap-2 rounded-lg border border-slate-700 px-3 py-2 text-[11px] font-semibold text-slate-300 disabled:opacity-50">
                            <Unlink size={14} /> Disconnect
                          </button>
                        </>
                      )}
                      {editable && (
                        <button
                          type="button"
                          onClick={() => testSpaBookingConnection(spa.id).then((result) => setConnectionStatus(result.missing.length ? `Not configured: ${result.missing.join(", ")}` : result.status.replace("_", " "))).catch(() => setConnectionStatus("connection failed"))}
                          className="inline-flex items-center gap-2 rounded-lg border border-slate-700 px-3 py-2 text-[11px] font-semibold text-slate-300 hover:border-cyan-400/40 hover:text-cyan-300"
                        >
                          <TestTube size={14} /> Test connection
                        </button>
                      )}
                      <span className="text-[11px] capitalize text-slate-400">Status: {connectionStatus ?? googleStatus?.status?.replace(/_/g, " ") ?? "not connected"}</span>
                    </div>
                    <label className="block">
                      <span className="mb-1.5 block text-xs font-medium text-slate-500">Legacy Calendar ID</span>
                      <input
                        type="text"
                        value={spa.booking_config?.google_calendar_id ?? ""}
                        disabled={!editable || Boolean(googleStatus?.connected)}
                        onChange={(event) => patch("booking_config", { ...spa.booking_config, google_calendar_id: event.target.value })}
                        placeholder="Used only for backwards compatibility"
                        className="w-full rounded-lg border border-slate-800 bg-[#07111f] px-3 py-2.5 text-sm text-slate-400 outline-none disabled:opacity-60"
                      />
                    </label>
                  </div>
                ) : PROVIDER_FIELDS[spa.booking_provider].map((field) => (
                  <label key={field.key} className="block">
                    <span className="mb-1.5 block text-xs font-medium text-slate-400">{field.label}</span>
                    <input
                      type={field.secret ? "password" : "text"}
                      value={spa.booking_config?.[field.key] ?? ""}
                      disabled={!editable}
                      onChange={(event) => patch("booking_config", { ...spa.booking_config, [field.key]: event.target.value })}
                      placeholder={field.secret ? "Enter a new secret" : undefined}
                      className="w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white outline-none disabled:opacity-60"
                    />
                  </label>
                ))}
                {spa.booking_provider !== "google_calendar" && (
                <div className="flex items-center gap-3 pt-1">
                  {editable && (
                    <button
                      type="button"
                      onClick={() => testSpaBookingConnection(spa.id).then((result) => setConnectionStatus(result.missing.length ? `Not configured: ${result.missing.join(", ")}` : result.status.replace("_", " "))).catch(() => setConnectionStatus("connection failed"))}
                      className="inline-flex items-center gap-2 rounded-lg border border-slate-700 px-3 py-2 text-[11px] font-semibold text-slate-300 hover:border-cyan-400/40 hover:text-cyan-300"
                    >
                      <TestTube size={14} /> Test connection
                    </button>
                  )}
                  <span className="text-[11px] capitalize text-slate-400">
                    Status: {connectionStatus ?? (spa.booking_provider_configured ? "configured" : "not configured")}
                  </span>
                </div>
                )}
              </div>
            </Panel>

            <Panel
              title="Receptionist persona"
              subtitle="Prepended to the shared voice rules for every call."
            >
              <textarea
                rows={10}
                value={spa.grok_system_prompt ?? ""}
                disabled={!editable}
                onChange={(event) => patch("grok_system_prompt", event.target.value)}
                placeholder="Greet guests warmly, mention the tea bar, and always confirm the therapist by name."
                className="w-full resize-y rounded-lg border border-slate-700 bg-[#07111f] px-3 py-3 font-mono text-xs leading-relaxed text-slate-200 outline-none disabled:opacity-60"
              />
            </Panel>

            <Panel title="Opening hours" subtitle="Bookings outside these are refused.">
              <div className="space-y-2">
                {DAYS.map(([key, label]) => {
                  const window = spa.business_hours[key]?.[0];
                  return (
                    <div key={key} className="flex items-center gap-2">
                      <span className="w-20 text-[11px] text-slate-400">{label}</span>
                      <input
                        type="time"
                        value={window?.open ?? ""}
                        disabled={!editable}
                        onChange={(event) => setHours(key, "open", event.target.value)}
                        className="rounded-lg border border-slate-700 bg-[#07111f] px-2 py-1.5 text-xs text-slate-200 outline-none disabled:opacity-60"
                      />
                      <span className="text-[11px] text-slate-600">to</span>
                      <input
                        type="time"
                        value={window?.close ?? ""}
                        disabled={!editable}
                        onChange={(event) => setHours(key, "close", event.target.value)}
                        className="rounded-lg border border-slate-700 bg-[#07111f] px-2 py-1.5 text-xs text-slate-200 outline-none disabled:opacity-60"
                      />
                      {!window && (
                        <span className="text-[10px] text-slate-600">closed</span>
                      )}
                    </div>
                  );
                })}
              </div>
            </Panel>

            <Panel
              title="Team"
              subtitle="Also sets how many appointments can run at once."
            >
              <div className="space-y-2">
                {spa.staff.map((member, index) => (
                  <div key={index} className="flex items-center gap-2">
                    <input
                      value={member.name}
                      disabled={!editable}
                      onChange={(event) =>
                        patch(
                          "staff",
                          spa.staff.map((item, i) =>
                            i === index ? { ...item, name: event.target.value } : item
                          )
                        )
                      }
                      placeholder="Name"
                      className="flex-1 rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2 text-xs text-slate-200 outline-none disabled:opacity-60"
                    />
                    <input
                      value={member.role ?? ""}
                      disabled={!editable}
                      onChange={(event) =>
                        patch(
                          "staff",
                          spa.staff.map((item, i) =>
                            i === index ? { ...item, role: event.target.value } : item
                          )
                        )
                      }
                      placeholder="Role"
                      className="flex-1 rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2 text-xs text-slate-200 outline-none disabled:opacity-60"
                    />
                    <input
                      value={(member.services ?? []).join(", ")}
                      disabled={!editable}
                      onChange={(event) =>
                        patch(
                          "staff",
                          spa.staff.map((item, i) =>
                            i === index
                              ? {
                                  ...item,
                                  services: event.target.value
                                    .split(",")
                                    .map((part) => part.trim())
                                    .filter(Boolean),
                                }
                              : item
                          )
                        )
                      }
                      placeholder="Services: facial, massage, back"
                      className="flex-[1.4] rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2 text-xs text-slate-200 outline-none disabled:opacity-60"
                    />
                  </div>
                ))}
                {editable && (
                  <button
                    onClick={() =>
                      patch("staff", [...spa.staff, { name: "", role: "", services: [] }])
                    }
                    className="mt-2 w-full rounded-lg border border-slate-700 py-2 text-[11px] font-semibold text-slate-400 hover:border-cyan-400/40 hover:text-cyan-300"
                  >
                    Add team member
                  </button>
                )}
              </div>
            </Panel>

            <Panel title="Policies, upsells, and Booking CC" subtitle="The receptionist may only quote these configured facts. It will never invent packages, prices, or card rules.">
              <label className="block">
                <span className="mb-2 block text-xs font-medium text-slate-400">Cancellation policy</span>
                <textarea
                  rows={3}
                  value={spa.cancellation_policy ?? ""}
                  disabled={!editable}
                  onChange={(event) => patch("cancellation_policy", event.target.value)}
                  placeholder="Leave blank if the receptionist should say it cannot verify a policy."
                  className="w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white outline-none disabled:opacity-60"
                />
              </label>
              <label className="mt-4 block">
                <span className="mb-2 block text-xs font-medium text-slate-400">Amenities (one per line)</span>
                <textarea
                  rows={3}
                  value={(spa.amenities ?? []).join("\n")}
                  disabled={!editable}
                  onChange={(event) =>
                    patch(
                      "amenities",
                      event.target.value.split("\n").map((line) => line.trim()).filter(Boolean)
                    )
                  }
                  className="w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white outline-none disabled:opacity-60"
                />
              </label>
              <label className="mt-4 flex items-center gap-2 text-xs text-slate-300">
                <input
                  type="checkbox"
                  checked={Boolean(spa.payment_policy?.card_required)}
                  disabled={!editable}
                  onChange={(event) =>
                    patch("payment_policy", {
                      ...(spa.payment_policy ?? { card_required: false, collection_mode: "none" }),
                      card_required: event.target.checked,
                    })
                  }
                />
                Card required (Booking CC)
              </label>
              <p className="mt-1 text-[10px] text-slate-500">
                Booking CC is the spa&apos;s card-on-file policy. The AI never takes a card number by voice.
              </p>
              <label className="mt-3 block">
                <span className="mb-1.5 block text-xs font-medium text-slate-400">Card collection mode</span>
                <select
                  value={spa.payment_policy?.collection_mode ?? "none"}
                  disabled={!editable}
                  onChange={(event) =>
                    patch("payment_policy", {
                      ...(spa.payment_policy ?? { card_required: false, collection_mode: "none" }),
                      collection_mode: event.target.value as "none" | "at_spa" | "square_link" | "secure_sms_link",
                    })
                  }
                  className="w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-slate-200 outline-none disabled:opacity-60"
                >
                  <option value="none">None — do not ask for a card</option>
                  <option value="at_spa">Taken at the spa</option>
                  <option value="square_link">Square secure link (never spoken PAN)</option>
                  <option value="secure_sms_link">Secure SMS link — save card on file, no charge</option>
                </select>
              </label>
              <div className="mt-4 space-y-2">
                <span className="block text-xs font-medium text-slate-400">Upsell rules</span>
                {(spa.upsell_rules ?? []).map((rule, index) => (
                  <div key={index} className="space-y-1 rounded-lg border border-slate-800 p-2">
                    <input
                      value={rule.base_service}
                      disabled={!editable}
                      placeholder="Base service, e.g. HydroLux5 Facial - Face Only"
                      onChange={(event) =>
                        patch(
                          "upsell_rules",
                          (spa.upsell_rules ?? []).map((item, i) =>
                            i === index ? { ...item, base_service: event.target.value } : item
                          )
                        )
                      }
                      className="w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2 text-xs text-slate-200 outline-none disabled:opacity-60"
                    />
                    <input
                      value={(rule.allowed_upsells ?? []).join(", ")}
                      disabled={!editable}
                      placeholder="Allowed upsells, comma-separated"
                      onChange={(event) =>
                        patch(
                          "upsell_rules",
                          (spa.upsell_rules ?? []).map((item, i) =>
                            i === index
                              ? {
                                  ...item,
                                  allowed_upsells: event.target.value
                                    .split(",")
                                    .map((part) => part.trim())
                                    .filter(Boolean),
                                }
                              : item
                          )
                        )
                      }
                      className="w-full rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2 text-xs text-slate-200 outline-none disabled:opacity-60"
                    />
                  </div>
                ))}
                {editable && (
                  <button
                    type="button"
                    onClick={() =>
                      patch("upsell_rules", [
                        ...(spa.upsell_rules ?? []),
                        { base_service: "", allowed_upsells: [] },
                      ])
                    }
                    className="w-full rounded-lg border border-slate-700 py-2 text-[11px] font-semibold text-slate-400 hover:border-cyan-400/40 hover:text-cyan-300"
                  >
                    Add upsell rule
                  </button>
                )}
              </div>
            </Panel>
          </div>
        )}
      </StateBlock>
    </>
  );
}
