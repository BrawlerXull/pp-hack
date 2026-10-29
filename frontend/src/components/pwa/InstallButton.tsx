"use client";

import { Download, Share } from "lucide-react";
import { useState } from "react";
import { useInstall } from "@/lib/pwa";

/** "Install app" where the browser supports it; on iPhone, how to add it to the Home Screen. */
export function InstallButton({ className = "" }: { className?: string }) {
  const { mode, install } = useInstall();
  const [showHelp, setShowHelp] = useState(false);
  if (!mode) return null;
  if (mode === "prompt")
    return (
      <button type="button" onClick={install} className={className}>
        <Download className="size-4 shrink-0" aria-hidden /> Install app
      </button>
    );
  return (
    <div>
      <button type="button" onClick={() => setShowHelp((v) => !v)} aria-expanded={showHelp} className={className}>
        <Download className="size-4 shrink-0" aria-hidden /> Install app
      </button>
      {showHelp && (
        <p className="mt-1 px-3 text-xs text-ink-3">
          Tap <Share className="inline size-3.5 align-text-bottom" aria-label="Share" /> in Safari, then <strong className="font-medium text-ink-2">Add to Home Screen</strong>.
        </p>
      )}
    </div>
  );
}
