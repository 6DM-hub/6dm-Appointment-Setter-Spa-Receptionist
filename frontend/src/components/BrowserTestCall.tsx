import { useEffect, useRef, useState } from "react";
import { Device, type Call as TwilioCall } from "@twilio/voice-sdk";
import { Mic, MicOff, Phone, PhoneOff } from "lucide-react";
import { apiClient, getApiErrorMessage } from "../api/client";
import { useAuth } from "../auth/AuthContext";

type ConnectionState =
  | "idle"
  | "connecting"
  | "connected"
  | "ended"
  | "error";

const formatDuration = (seconds: number) => {
  const mins = Math.floor(seconds / 60)
    .toString()
    .padStart(2, "0");

  const secs = (seconds % 60)
    .toString()
    .padStart(2, "0");

  return `${mins}:${secs}`;
};

export default function BrowserTestCall() {
  const { effectiveTenantId } = useAuth();

  const deviceRef = useRef<Device | null>(null);
  const callRef = useRef<TwilioCall | null>(null);

  const [status, setStatus] = useState<ConnectionState>("idle");
  const [duration, setDuration] = useState(0);
  const [muted, setMuted] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /**
   * Destroy the current Twilio device.
   */
  const destroyDevice = () => {
    if (!deviceRef.current) {
      return;
    }

    try {
      console.log("[BrowserTestCall] Destroying Twilio device");

      deviceRef.current.destroy();
    } catch (err) {
      console.warn(
        "[BrowserTestCall] Error while destroying device",
        err
      );
    }

    deviceRef.current = null;
  };

  /**
   * Clean up call + Twilio Device.
   */
  const cleanup = () => {
    const call = callRef.current;

    /*
     * Clear the ref BEFORE disconnecting.
     * This prevents disconnect-event cleanup loops.
     */
    callRef.current = null;

    if (call) {
      try {
        const callStatus = call.status();

        console.log(
          "[BrowserTestCall] Cleaning up call. Current status:",
          callStatus
        );

        if (callStatus !== "closed") {
          call.disconnect();
        }
      } catch (err) {
        console.warn(
          "[BrowserTestCall] Error while disconnecting call",
          err
        );
      }
    }

    destroyDevice();
    setMuted(false);
  };

  /**
   * Component unmount cleanup.
   */
  useEffect(() => {
    return () => {
      const call = callRef.current;
      callRef.current = null;

      if (call) {
        try {
          call.disconnect();
        } catch {
          // Ignore teardown race.
        }
      }

      if (deviceRef.current) {
        try {
          deviceRef.current.destroy();
        } catch {
          // Ignore teardown race.
        }

        deviceRef.current = null;
      }
    };
  }, []);

  /**
   * Call duration timer.
   */
  useEffect(() => {
    if (status !== "connected") {
      return;
    }

    const interval = window.setInterval(() => {
      setDuration((current) => current + 1);
    }, 1000);

    return () => {
      window.clearInterval(interval);
    };
  }, [status]);

  /**
   * Start browser → Twilio → AI receptionist call.
   */
  const startCall = async () => {
    if (!effectiveTenantId) {
      setError(
        "Select a tenant before starting the browser test call."
      );
      setStatus("error");
      return;
    }

    /*
     * Make sure an old Device doesn't still exist.
     */
    cleanup();

    setError(null);
    setMuted(false);
    setDuration(0);
    setStatus("connecting");

    console.log("========================================");
    console.log("[BrowserTestCall] START TEST CALL");
    console.log(
      "[BrowserTestCall] Tenant:",
      effectiveTenantId
    );
    console.log("========================================");

    try {
      /**
       * STEP 1
       * Verify microphone permission.
       *
       * Twilio will acquire its own microphone stream when
       * device.connect() starts the call.
       */
      console.log(
        "[BrowserTestCall] Requesting microphone permission..."
      );

      const permissionStream =
        await navigator.mediaDevices.getUserMedia({
          audio: true,
        });

      console.log(
        "[BrowserTestCall] Microphone permission granted"
      );

      /*
       * Stop this temporary permission-check stream.
       * Twilio will create the real call stream.
       */
      permissionStream
        .getTracks()
        .forEach((track) => track.stop());

      /**
       * STEP 2
       * Get temporary Twilio Voice access token.
       */
      console.log(
        "[BrowserTestCall] Requesting Twilio voice token..."
      );

      const { data } = await apiClient.get(
        "/api/v1/telephony/voice/token",
        {
          headers: {
            "X-Tenant-Id": effectiveTenantId,
          },
        }
      );

      if (!data?.token) {
        throw new Error(
          "The server did not return a Twilio Voice token."
        );
      }

      console.log(
        "[BrowserTestCall] Twilio token received"
      );

      /**
       * STEP 3
       * Create Twilio Device.
       *
       * IMPORTANT:
       * We DO NOT wait for "registered".
       *
       * device.register() is for receiving incoming calls.
       * For an outbound browser call, device.connect()
       * opens the signaling connection automatically.
       */
      console.log(
        "[BrowserTestCall] Creating Twilio Device..."
      );

      const device = new Device(data.token, {
        logLevel: 1,
        closeProtection: true,
      });

      deviceRef.current = device;

      /**
       * DEVICE ERROR
       */
      device.on("error", (err) => {
        console.error(
          "[BrowserTestCall] TWILIO DEVICE ERROR:",
          err
        );

        const message =
          (err as { message?: string })?.message ||
          getApiErrorMessage(
            err,
            "Unable to initialize the Twilio browser device."
          );

        setError(message);
        setStatus("error");

        callRef.current = null;
        destroyDevice();
      });

      /**
       * TOKEN REFRESH
       */
      device.on("tokenWillExpire", async () => {
        console.log(
          "[BrowserTestCall] Twilio token will expire. Refreshing..."
        );

        try {
          const refreshed = await apiClient.get(
            "/api/v1/telephony/voice/token",
            {
              headers: {
                "X-Tenant-Id": effectiveTenantId,
              },
            }
          );

          if (!refreshed.data?.token) {
            throw new Error(
              "The refreshed Twilio token was empty."
            );
          }

          device.updateToken(refreshed.data.token);

          console.log(
            "[BrowserTestCall] Twilio token refreshed"
          );
        } catch (refreshError) {
          console.error(
            "[BrowserTestCall] TOKEN REFRESH ERROR:",
            refreshError
          );

          const message = getApiErrorMessage(
            refreshError,
            "The temporary Twilio token expired."
          );

          setError(message);
          setStatus("error");

          cleanup();
        }
      });

      /**
       * STEP 4
       * Start the outgoing Twilio browser call.
       */
      console.log(
        "[BrowserTestCall] Calling device.connect()..."
      );

      const call = await device.connect({
        params: {
          tenant_id: effectiveTenantId,
          to: "browser-test",
        },
        rtcConstraints: {
          audio: true,
        },
      });

      if (!call) {
        throw new Error(
          "Twilio did not return a call object."
        );
      }

      callRef.current = call;

      console.log(
        "[BrowserTestCall] Twilio Call object created"
      );

      console.log(
        "[BrowserTestCall] Initial call status:",
        call.status()
      );

      /**
       * CALL ACCEPTED
       *
       * For an outgoing call, Twilio emits "accept"
       * after the media session is established.
       */
      call.on("accept", () => {
        console.log("========================================");
        console.log(
          "[BrowserTestCall] CALL ACCEPTED / CONNECTED"
        );
        console.log(
          "[BrowserTestCall] Status:",
          call.status()
        );
        console.log("========================================");

        setError(null);
        setStatus("connected");
      });

      /**
       * CALL RINGING
       */
      call.on("ringing", () => {
        console.log(
          "[BrowserTestCall] Call is ringing..."
        );
      });

      /**
       * MUTE / UNMUTE
       *
       * Twilio uses ONE event for both.
       */
      call.on("mute", (isMuted: boolean) => {
        console.log(
          "[BrowserTestCall] Microphone muted:",
          isMuted
        );

        setMuted(isMuted);
      });

      /**
       * CALL DISCONNECTED
       */
      call.on("disconnect", () => {
        console.log(
          "[BrowserTestCall] CALL DISCONNECTED"
        );

        /*
         * Clear first so cleanup doesn't try
         * to disconnect the same Call again.
         */
        callRef.current = null;

        setMuted(false);
        setStatus("ended");

        destroyDevice();
      });

      /**
       * CALL CANCELED
       */
      call.on("cancel", () => {
        console.log(
          "[BrowserTestCall] CALL CANCELED"
        );

        callRef.current = null;

        setMuted(false);
        setStatus("ended");

        destroyDevice();
      });

      /**
       * CALL ERROR
       */
      call.on("error", (err) => {
        console.error("========================================");
        console.error(
          "[BrowserTestCall] TWILIO CALL ERROR:",
          err
        );
        console.error("========================================");

        const twilioError = err as {
          code?: number;
          message?: string;
          description?: string;
          explanation?: string;
          causes?: string[];
          solutions?: string[];
        };

        console.error(
          "[BrowserTestCall] Error code:",
          twilioError.code
        );

        console.error(
          "[BrowserTestCall] Description:",
          twilioError.description
        );

        console.error(
          "[BrowserTestCall] Explanation:",
          twilioError.explanation
        );

        console.error(
          "[BrowserTestCall] Causes:",
          twilioError.causes
        );

        console.error(
          "[BrowserTestCall] Solutions:",
          twilioError.solutions
        );

        const message =
          twilioError.message ||
          twilioError.description ||
          getApiErrorMessage(
            err,
            "The browser call failed."
          );

        callRef.current = null;

        setError(message);
        setStatus("error");
        setMuted(false);

        destroyDevice();
      });

      console.log(
        "[BrowserTestCall] Waiting for Twilio accept event..."
      );
    } catch (err) {
      console.error("========================================");
      console.error(
        "[BrowserTestCall] START CALL FAILED:",
        err
      );
      console.error("========================================");

      const directMessage =
        err instanceof Error
          ? err.message
          : null;

      const message =
        directMessage ||
        getApiErrorMessage(
          err,
          "Microphone access or the temporary Twilio token is unavailable."
        );

      setError(message);
      setStatus("error");
      setMuted(false);

      cleanup();
    }
  };

  /**
   * Mute / unmute microphone.
   */
  const toggleMute = () => {
    const call = callRef.current;

    if (!call || status !== "connected") {
      return;
    }

    try {
      const currentlyMuted = call.isMuted();

      console.log(
        "[BrowserTestCall] Toggle mute:",
        !currentlyMuted
      );

      /*
       * The Twilio "mute" event updates React state.
       */
      call.mute(!currentlyMuted);
    } catch (err) {
      console.error(
        "[BrowserTestCall] Unable to toggle mute:",
        err
      );
    }
  };

  /**
   * End the current call.
   */
  const hangUp = () => {
    console.log(
      "[BrowserTestCall] User clicked End Call"
    );

    const call = callRef.current;

    /*
     * Clear first to prevent event cleanup loops.
     */
    callRef.current = null;

    if (call) {
      try {
        call.disconnect();
      } catch (err) {
        console.warn(
          "[BrowserTestCall] Hangup disconnect error:",
          err
        );
      }
    }

    destroyDevice();

    setMuted(false);
    setStatus("ended");
  };

  const primaryLabel =
    status === "connecting"
      ? "Connecting..."
      : status === "connected"
        ? "Connected"
        : status === "ended"
          ? "Ended"
          : status === "error"
            ? "Error"
            : "Ready";

  return (
    <div className="rounded-2xl border border-slate-800 bg-[#0b1a2c] p-5">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-[10px] font-bold uppercase tracking-[.22em] text-cyan-300">
            AI Receptionist Test
          </p>

          <h3 className="mt-2 text-lg font-semibold text-white">
            Browser microphone test
          </h3>
        </div>

        <button
          type="button"
          onClick={startCall}
          disabled={
            status === "connecting" ||
            status === "connected"
          }
          className="rounded-xl bg-cyan-500 px-4 py-2 text-sm font-medium text-slate-950 transition hover:bg-cyan-400 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400"
        >
          {status === "connecting"
            ? "Connecting..."
            : "Start Test Call"}
        </button>
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-3 text-sm text-slate-300">
        <span className="inline-flex items-center gap-2 rounded-full border border-slate-700 bg-slate-900/70 px-3 py-1.5">
          <span
            className={`h-2.5 w-2.5 rounded-full ${
              status === "connected"
                ? "bg-emerald-400"
                : status === "connecting"
                  ? "bg-amber-400"
                  : status === "error"
                    ? "bg-rose-400"
                    : "bg-slate-500"
            }`}
          />

          Status: {primaryLabel}
        </span>

        <span className="inline-flex items-center rounded-full border border-slate-700 bg-slate-900/70 px-3 py-1.5">
          Duration: {formatDuration(duration)}
        </span>
      </div>

      {error && (
        <div className="mt-4 rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-200">
          {error}
        </div>
      )}

      <div className="mt-5 flex flex-wrap gap-3">
        <button
          type="button"
          onClick={toggleMute}
          disabled={status !== "connected"}
          className="inline-flex items-center gap-2 rounded-xl border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 transition hover:border-cyan-400/50 hover:text-cyan-300 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {muted ? (
            <MicOff size={16} />
          ) : (
            <Mic size={16} />
          )}

          {muted ? "Unmute" : "Mute"}
        </button>

        <button
          type="button"
          onClick={hangUp}
          disabled={
            status === "idle" ||
            status === "ended" ||
            status === "error"
          }
          className="inline-flex items-center gap-2 rounded-xl border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-200 transition hover:border-rose-400 hover:bg-rose-500/20 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <PhoneOff size={16} />
          End Call
        </button>
      </div>

      <div className="mt-4 flex items-center justify-between gap-3 border-t border-slate-800 pt-3 text-xs text-slate-500">
        <span>
          Browser mic → Twilio → browser test route →
          existing realtime flow
        </span>

        <span className="inline-flex items-center gap-1 text-slate-400">
          <Phone size={12} />
          Browser call
        </span>
      </div>
    </div>
  );
}