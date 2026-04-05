"use client";

import { Toaster } from 'react-hot-toast';
import { Header } from '@/components/Header';
import { Sidebar } from '@/components/Sidebar';
import { Workspace } from '@/components/Workspace';
import { StatusBannerBar } from '@/components/StatusBannerBar';

export default function Home() {
  return (
    <div className="min-h-screen bg-neutral-950 text-neutral-200 font-sans flex flex-col">
      <Toaster position="top-right" toastOptions={{
        style: {
          background: '#1f2937',
          color: '#f3f4f6',
          border: '1px solid #374151'
        }
      }} />
      <Header />
      <StatusBannerBar />
      <main className="flex-1 flex overflow-hidden">
        <Sidebar />
        <Workspace />
      </main>
    </div>
  );
}
