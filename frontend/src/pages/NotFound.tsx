import { Link } from "react-router-dom";

export default function NotFound() {
  return (
    <div className="grid place-items-center py-24 text-center">
      <div>
        <h2 className="font-display text-4xl font-semibold text-white">404</h2>
        <p className="mt-2 text-sm text-slate-400">
          That page isn't part of your workspace.
        </p>
        <Link
          to="/"
          className="mt-6 inline-block rounded-lg bg-cyan-400 px-4 py-2.5 text-xs font-bold text-slate-950 hover:bg-cyan-300"
        >
          Back to the command center
        </Link>
      </div>
    </div>
  );
}
