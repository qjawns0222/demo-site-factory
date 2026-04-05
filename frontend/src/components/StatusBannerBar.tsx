"use client";

import { useWorkflowStore } from '../store/useWorkflowStore';

export function StatusBannerBar() {
  const { statusBanners, dismissBanner } = useWorkflowStore();

  if (statusBanners.length === 0) return null;

  return (
    <div className="flex flex-col gap-1 px-3 py-2 border-b border-neutral-800 bg-neutral-950 shrink-0">
      {statusBanners.map((banner, i) => (
        <div
          key={i}
          className={`flex items-start gap-2 px-3 py-2 rounded-lg text-sm font-medium
            ${banner.type === 'success' ? 'bg-green-950/60 border border-green-800/50 text-green-300' : ''}
            ${banner.type === 'error' ? 'bg-red-950/60 border border-red-800/50 text-red-300' : ''}
            ${banner.type === 'info' ? 'bg-blue-950/60 border border-blue-800/50 text-blue-300' : ''}
          `}
        >
          <span className="shrink-0 mt-0.5">
            {banner.type === 'success' && '✓'}
            {banner.type === 'error' && '✕'}
            {banner.type === 'info' && 'ℹ'}
          </span>
          <span className="flex-1 leading-snug">{banner.message}</span>
          <button
            onClick={() => dismissBanner(i)}
            className="shrink-0 ml-2 opacity-50 hover:opacity-100 transition text-lg leading-none"
            aria-label="닫기"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
