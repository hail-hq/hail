'use client';

import { useEffect, useRef, useState } from 'react';

export function TableWrap({ children }: { children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [canScrollLeft, setCanScrollLeft] = useState(false);
  const [canScrollRight, setCanScrollRight] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    const update = () => {
      setCanScrollLeft(el.scrollLeft > 1);
      setCanScrollRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 1);
    };

    update();
    el.addEventListener('scroll', update, { passive: true });
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => {
      el.removeEventListener('scroll', update);
      ro.disconnect();
    };
  }, []);

  return (
    <div className="table-wrap-outer">
      <div className="table-wrap" ref={ref}>
        {children}
      </div>
      <span className={`scroll-hint scroll-hint-left${canScrollLeft ? ' is-visible' : ''}`} aria-hidden="true">
        ‹
      </span>
      <span className={`scroll-hint scroll-hint-right${canScrollRight ? ' is-visible' : ''}`} aria-hidden="true">
        ›
      </span>
    </div>
  );
}
