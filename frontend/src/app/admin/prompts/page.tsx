"use client";

import { useState, useEffect } from 'react';
import toast, { Toaster } from 'react-hot-toast';

interface Prompt {
  step_id: number;
  content: string;
}

export default function AdminPromptsPage() {
  const [prompts, setPrompts] = useState<Prompt[]>([]);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editContent, setEditContent] = useState('');
  const [token, setToken] = useState('');
  const [authenticated, setAuthenticated] = useState(false);
  const [saving, setSaving] = useState(false);

  const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

  const fetchPrompts = async (adminToken: string) => {
    try {
      const res = await fetch(`${API_URL}/api/admin/prompts`, {
        headers: { 'x-admin-token': adminToken }
      });
      if (res.status === 401 || res.status === 503) {
        const data = await res.json();
        toast.error(data.detail || '인증 실패');
        return false;
      }
      const data = await res.json();
      setPrompts(data.prompts || []);
      return true;
    } catch {
      toast.error('서버 연결 실패');
      return false;
    }
  };

  const handleLogin = async () => {
    const ok = await fetchPrompts(token);
    if (ok) setAuthenticated(true);
  };

  const startEdit = (prompt: Prompt) => {
    setEditingId(prompt.step_id);
    setEditContent(prompt.content);
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditContent('');
  };

  const savePrompt = async (step_id: number) => {
    setSaving(true);
    try {
      const res = await fetch(`${API_URL}/api/admin/prompts/${step_id}`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          'x-admin-token': token,
        },
        body: JSON.stringify({ content: editContent }),
      });
      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.detail || '저장 실패');
      }
      setPrompts(prev => prev.map(p => p.step_id === step_id ? { ...p, content: editContent } : p));
      setEditingId(null);
      toast.success(`Step ${step_id} 프롬프트 저장 완료`);
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setSaving(false);
    }
  };

  if (!authenticated) {
    return (
      <div className="min-h-screen bg-neutral-950 flex items-center justify-center">
        <Toaster />
        <div className="bg-neutral-900 border border-neutral-800 rounded-xl p-8 w-full max-w-sm">
          <h1 className="text-white font-bold text-xl mb-6">Admin — Prompt Manager</h1>
          <input
            type="password"
            placeholder="ADMIN_TOKEN 입력"
            value={token}
            onChange={e => setToken(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleLogin()}
            className="w-full bg-neutral-800 border border-neutral-700 text-white rounded px-3 py-2 outline-none focus:ring-2 focus:ring-rose-500 font-mono text-sm mb-4"
          />
          <button
            onClick={handleLogin}
            className="w-full bg-rose-600 hover:bg-rose-500 text-white py-2 rounded font-bold transition"
          >
            로그인
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-neutral-950 text-white">
      <Toaster />
      <header className="border-b border-neutral-800 bg-neutral-900 px-8 py-4 flex justify-between items-center">
        <h1 className="text-xl font-bold">
          <span className="bg-rose-700 px-2 py-0.5 rounded text-sm mr-2">Admin</span>
          Prompt Manager
        </h1>
        <a href="/" className="text-neutral-500 hover:text-white text-sm transition">← 메인으로</a>
      </header>

      <main className="max-w-4xl mx-auto px-6 py-8 space-y-4">
        <p className="text-neutral-500 text-sm">각 단계의 프롬프트를 수정합니다. 저장 즉시 다음 스트림부터 적용됩니다.</p>

        {prompts.map(prompt => (
          <div key={prompt.step_id} className="border border-neutral-800 rounded-xl overflow-hidden">
            <div className="bg-neutral-900 px-4 py-3 flex justify-between items-center border-b border-neutral-800">
              <span className="font-mono text-sm text-blue-400">Step {prompt.step_id}</span>
              {editingId === prompt.step_id ? (
                <div className="flex gap-2">
                  <button
                    onClick={cancelEdit}
                    className="text-neutral-400 hover:text-white text-sm px-3 py-1 rounded border border-neutral-700 transition"
                  >
                    취소
                  </button>
                  <button
                    onClick={() => savePrompt(prompt.step_id)}
                    disabled={saving}
                    className="bg-blue-600 hover:bg-blue-500 text-white text-sm px-3 py-1 rounded font-bold transition disabled:opacity-50"
                  >
                    {saving ? '저장 중...' : '저장'}
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => startEdit(prompt)}
                  className="text-neutral-400 hover:text-white text-sm px-3 py-1 rounded border border-neutral-700 transition"
                >
                  편집
                </button>
              )}
            </div>

            {editingId === prompt.step_id ? (
              <textarea
                className="w-full bg-neutral-950 text-neutral-300 p-4 outline-none font-mono text-sm leading-relaxed resize-none"
                rows={12}
                value={editContent}
                onChange={e => setEditContent(e.target.value)}
                autoFocus
              />
            ) : (
              <pre className="p-4 text-neutral-500 font-mono text-xs leading-relaxed whitespace-pre-wrap max-h-48 overflow-y-auto">
                {prompt.content}
              </pre>
            )}
          </div>
        ))}
      </main>
    </div>
  );
}
