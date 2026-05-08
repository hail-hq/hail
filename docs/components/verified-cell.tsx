import { isStale, daysSince } from '@/lib/staleness';

export function VerifiedCell({ date }: { date: string }) {
  return (
    <span>
      <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--color-mute)' }}>
        {date}
      </span>
      {isStale(date) && <span className="stale-pill">stale {daysSince(date)}d</span>}
    </span>
  );
}
