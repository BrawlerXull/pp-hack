"use client";

import Link from "next/link";
import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { api, type DeviceRow, type Devices } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import { Badge, Button, Empty } from "./ui";

/** Blocked count for the card title, from the same request as the list. */
export function useDeviceCounts() {
  const { data } = useSWR<Devices>("/dashboard/devices", { refreshInterval: 15_000 });
  return data ? { blocked: data.blocked.length, trusted: data.trusted.length } : null;
}

// Blocks expire on their own (24 h). "Unblock" also trusts the device, so the guard does not block it
// again a few seconds later and never refunds its payments while the trust lasts.
export function DevicesList() {
  const { mutate } = useSWRConfig();
  const { data } = useSWR<Devices>("/dashboard/devices", { refreshInterval: 15_000 });
  if (!data) return <div className="h-16 animate-pulse rounded-xl bg-slate-100" />;
  if (data.blocked.length === 0 && data.trusted.length === 0) return <Empty>No devices blocked.</Empty>;
  const refresh = () => {
    mutate("/dashboard/devices");
    mutate("/dashboard/summary");
  };
  return (
    <ul className="divide-y divide-slate-100">
      {data.blocked.map((d) => (
        <DeviceItem key={`b-${d.fingerprint}`} device={d} blocked onChange={refresh} />
      ))}
      {data.trusted.map((d) => (
        <DeviceItem key={`t-${d.fingerprint}`} device={d} onChange={refresh} />
      ))}
    </ul>
  );
}

function DeviceItem({ device, blocked = false, onChange }: { device: DeviceRow; blocked?: boolean; onChange: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function unblock() {
    setBusy(true);
    setError(null);
    try {
      await api(`/dashboard/devices/${encodeURIComponent(device.fingerprint)}/unblock`, { method: "POST" });
      onChange();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }
  const expires = new Date(device.expires_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  return (
    <li className="flex flex-wrap items-center justify-between gap-3 py-2 text-sm">
      <div className="min-w-0">
        <p className="flex items-center gap-2">
          <Badge tone={blocked ? "bad" : "info"}>{blocked ? "Blocked" : "Trusted"}</Badge>
          <code className="truncate font-mono text-xs text-slate-700">{device.fingerprint.slice(0, 12)}…</code>
        </p>
        <p className="mt-0.5 text-xs text-slate-500">
          {device.reason ?? "no reason recorded"} · {timeAgo(device.created_at)} · until {expires}
          {device.incident_id && (
            <>
              {" · "}
              <Link href={`/guard/incidents/${device.incident_id}`} className="text-guard hover:underline">
                incident
              </Link>
            </>
          )}
        </p>
        {error && <p className="text-xs text-red-700">{error}</p>}
      </div>
      {blocked && (
        <Button variant="secondary" className="px-2 py-1 text-xs" disabled={busy} onClick={unblock}>
          Unblock and trust
        </Button>
      )}
    </li>
  );
}
