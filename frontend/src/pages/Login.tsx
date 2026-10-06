import { useState } from "react";
import { AudioLines, Lock, Mail } from "lucide-react";
import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

export default function Login() {
  const { user, loading, error, signIn } = useAuth();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

  if (user) {
    const from = (location.state as { from?: { pathname: string } } | null)?.from;
    return <Navigate to={from?.pathname || "/"} replace />;
  }

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    try {
      await signIn(email, password);
    } catch {
      // `error` from the context renders below; nothing else to do here.
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="grid min-h-screen place-items-center bg-[#07111f] px-4 text-slate-100">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-2xl border border-slate-800 bg-[#0b1a2c] p-7"
      >
        <span className="mb-6 grid h-11 w-11 place-items-center rounded-xl bg-cyan-400 text-slate-950">
          <AudioLines size={20} />
        </span>
        <h1 className="font-display text-xl font-semibold tracking-tight text-white">
          Sign in
        </h1>
        <p className="mt-1 text-xs text-slate-500">
          6DM staff and spa partners use the same sign-in. Your workspace is
          chosen for you.
        </p>

        <label className="mt-6 block">
          <span className="mb-2 block text-xs font-medium text-slate-400">Email</span>
          <div className="flex items-center rounded-lg border border-slate-700 bg-[#07111f] px-3">
            <Mail size={15} className="text-slate-500" />
            <input
              required
              type="email"
              autoComplete="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              className="w-full bg-transparent px-3 py-3 text-sm text-white outline-none placeholder:text-slate-600"
              placeholder="you@example.com"
            />
          </div>
        </label>

        <label className="mt-4 block">
          <span className="mb-2 block text-xs font-medium text-slate-400">Password</span>
          <div className="flex items-center rounded-lg border border-slate-700 bg-[#07111f] px-3">
            <Lock size={15} className="text-slate-500" />
            <input
              required
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              className="w-full bg-transparent px-3 py-3 text-sm text-white outline-none placeholder:text-slate-600"
              placeholder="••••••••"
            />
          </div>
        </label>

        {error && (
          <p className="mt-4 rounded-lg border border-rose-400/20 bg-rose-400/5 px-3 py-2 text-xs text-rose-300">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={submitting || loading}
          className="mt-6 w-full rounded-lg bg-cyan-400 py-3 text-xs font-bold text-slate-950 transition hover:bg-cyan-300 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
