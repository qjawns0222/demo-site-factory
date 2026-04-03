"use client";

import { useState, useRef, useEffect } from 'react';
import { useWorkflowStore } from '../store/useWorkflowStore';
import toast from 'react-hot-toast';

export function Header() {
  const {
    domain, sessionId, setDomain, setSessionId, setSteps,
    isStreaming, isSynthesizing, steps, resetAll,
    isRunningAll, setIsRunningAll, setPendingStepId,
    generationMode, setGenerationMode,
  } = useWorkflowStore();
  const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

  const [sessions, setSessions] = useState<{session_id: string; domain: string}[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const historyRef = useRef<HTMLDivElement>(null);

  // 히스토리 드롭다운 외부 클릭 시 닫기
  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (historyRef.current && !historyRef.current.contains(e.target as Node)) {
        setShowHistory(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  const fetchWorkflow = async () => {
    try {
      const res = await fetch(`${API_URL}/api/workflow`);
      const data = await res.json();
      if (data.steps) {
        setSteps(data.steps.map((s: any) => ({ ...s, status: 'PENDING', content: '' })));
      }
    } catch {
      toast.error("서버에서 워크플로우를 가져오는데 실패했습니다.");
    }
  };

  const fetchSessions = async () => {
    try {
      const res = await fetch(`${API_URL}/api/sessions`);
      const data = await res.json();
      setSessions(data.sessions || []);
    } catch {
      toast.error("세션 히스토리를 불러오는데 실패했습니다.");
    }
  };

  const resetGeneration = async () => {
    if (!sessionId) return toast.error('활성화된 세션이 없습니다.');
    if (!confirm('정말로 로컬 세션을 완전히 삭제합니까? 데이터는 복구할 수 없습니다.')) return;

    try {
      const res = await fetch(`${API_URL}/api/session/${sessionId}`, { method: 'DELETE' });
      if (!res.ok) throw new Error('API 오류');
      resetAll();
      toast.success('세션이 완전 초기화되었습니다.');
      fetchWorkflow();
    } catch {
      toast.error('초기화 중 오류 발생');
    }
  };

  const startGeneration = async () => {
    if (!domain.trim()) return toast.error('도메인을 입력해주세요.');
    if (steps.length === 0) {
      await fetchWorkflow();
    }

    try {
      const req = await fetch(`${API_URL}/api/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ domain: domain.trim() })
      });
      if (!req.ok) {
        const errorData = await req.json();
        throw new Error(errorData.detail || 'API 호출 실패');
      }
      const data = await req.json();
      setSessionId(data.session_id);
      toast.success(`세션 할당 완료: ${data.session_id.slice(0, 8)}...`);
    } catch (err: any) {
      toast.error(`개시 실패: ${err.message}`);
    }
  };

  const runAll = async () => {
    if (!sessionId || steps.length === 0) return;
    if (isStreaming || isSynthesizing) return;

    // 완료되지 않은 첫 번째 단계부터 시작 (DONE 이후 재개 지원)
    const firstPending = steps.find(s => s.status !== 'DONE');
    if (!firstPending) {
      toast('모든 단계가 이미 완료되어 있습니다.', { icon: '✅' });
      return;
    }

    const doneCount = steps.filter(s => s.status === 'DONE').length;
    const resumeMsg = doneCount > 0
      ? `${doneCount}단계 완료됨 — ${firstPending.id}단계부터 이어서 실행합니다...`
      : '전체 자동 실행을 시작합니다...';

    setIsRunningAll(true);
    toast(resumeMsg, { icon: doneCount > 0 ? '▶️' : '🚀' });
    setPendingStepId(firstPending.id);
  };

  const restoreSession = async (sess: {session_id: string; domain: string}) => {
    if (isStreaming || isSynthesizing) {
      toast.error('진행 중인 작업이 있습니다. 완료 후 복원하세요.');
      return;
    }
    if (sessionId && !confirm('현재 세션을 버리고 선택한 세션으로 복원하시겠습니까?')) return;

    resetAll();
    setDomain(sess.domain);

    // 워크플로우 가져오기
    try {
      const res = await fetch(`${API_URL}/api/workflow`);
      const data = await res.json();
      if (data.steps) {
        setSteps(data.steps.map((s: any) => ({ ...s, status: 'PENDING', content: '' })));
      }
    } catch {}

    setSessionId(sess.session_id);
    setShowHistory(false);
    toast.success(`세션 복원: ${sess.domain}`);
  };

  const handleExportZip = () => {
    if (!sessionId) return toast.error('다운로드할 세션 데이터가 없습니다.');
    window.location.href = `${API_URL}/api/export/${sessionId}`;
  };

  const handlePreview = () => {
    if (!sessionId) return toast.error('프리뷰할 세션 데이터가 없습니다.');
    window.open(`${API_URL}/api/preview/${sessionId}`, '_blank');
  };

  const isBusy = isStreaming || isSynthesizing || isRunningAll;

  return (
    <header className="border-b border-neutral-800 bg-neutral-900 p-4 shrink-0">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        {/* 로고 */}
        <h1 className="text-xl font-bold tracking-tight text-white flex items-center gap-2 shrink-0">
          <span className="bg-rose-700 text-white px-2 py-0.5 rounded text-sm shadow-md shadow-rose-900/50">V8</span>
          AI Demo Site Factory
        </h1>

        {/* 메인 컨트롤 */}
        <div className="flex gap-2 flex-1 min-w-0 flex-wrap">
          {/* 모드 토글 (세션 없을 때만) */}
          {!sessionId && (
            <div className="flex rounded overflow-hidden border border-neutral-700 shrink-0">
              <button
                onClick={() => setGenerationMode('doc')}
                className={`px-3 py-2 text-xs font-semibold transition ${generationMode === 'doc' ? 'bg-blue-700 text-white' : 'bg-neutral-800 text-neutral-400 hover:bg-neutral-700'}`}
              >
                문서
              </button>
              <button
                onClick={() => setGenerationMode('code')}
                className={`px-3 py-2 text-xs font-semibold transition ${generationMode === 'code' ? 'bg-emerald-700 text-white' : 'bg-neutral-800 text-neutral-400 hover:bg-neutral-700'}`}
              >
                코드
              </button>
            </div>
          )}

          {/* 도메인 입력 */}
          <input
            type="text"
            className="flex-1 min-w-[160px] bg-neutral-800 border border-neutral-700 text-white rounded px-3 py-2 outline-none focus:ring-2 focus:ring-rose-500 font-mono text-sm"
            placeholder="도메인 주제 입력 (예: chat-service)"
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            disabled={isBusy || !!sessionId}
          />

          {/* 세션 히스토리 */}
          <div className="relative shrink-0" ref={historyRef}>
            <button
              onClick={() => { fetchSessions(); setShowHistory(v => !v); }}
              className="bg-neutral-800 border border-neutral-700 hover:bg-neutral-700 text-neutral-300 px-3 py-2 rounded text-sm transition"
              title="세션 히스토리"
            >
              🕐
            </button>
            {showHistory && (
              <div className="absolute right-0 top-full mt-1 w-64 bg-neutral-900 border border-neutral-700 rounded-lg shadow-2xl z-50 max-h-64 overflow-y-auto">
                <div className="p-2 text-[10px] uppercase tracking-wider text-neutral-500 border-b border-neutral-800">최근 세션</div>
                {sessions.length === 0 ? (
                  <div className="p-4 text-neutral-600 text-sm text-center">세션 없음</div>
                ) : (
                  sessions.map(sess => (
                    <button
                      key={sess.session_id}
                      onClick={() => restoreSession(sess)}
                      className="w-full text-left px-3 py-2 hover:bg-neutral-800 transition text-sm"
                    >
                      <div className="text-white font-mono">{sess.domain}</div>
                      <div className="text-neutral-500 text-[11px]">{sess.session_id.slice(0, 12)}...</div>
                    </button>
                  ))
                )}
              </div>
            )}
          </div>

          {/* Boot / Reset */}
          {sessionId ? (
            <button
              onClick={resetGeneration}
              disabled={isBusy}
              className="bg-red-900/30 text-red-500 border border-red-800/50 hover:bg-red-900/80 px-4 py-2 rounded font-bold transition disabled:opacity-50 text-sm shrink-0"
            >
              Reset
            </button>
          ) : (
            <button
              onClick={startGeneration}
              disabled={isBusy || !domain.trim()}
              className="bg-rose-600 hover:bg-rose-500 text-white px-4 py-2 rounded font-semibold transition disabled:opacity-50 shrink-0 text-sm"
            >
              Boot
            </button>
          )}

          {/* Run All */}
          {sessionId && (
            <button
              onClick={runAll}
              disabled={isBusy}
              className="bg-amber-600 hover:bg-amber-500 disabled:opacity-50 disabled:bg-neutral-800 disabled:text-neutral-500 text-white px-4 py-2 rounded font-semibold transition shrink-0 text-sm flex items-center gap-1"
            >
              {isRunningAll ? <span className="animate-spin">⚙</span> : '🚀'}
              {isRunningAll ? '실행 중...' : 'Run All'}
            </button>
          )}

          {/* 프리뷰 */}
          <button
            onClick={handlePreview}
            disabled={!sessionId}
            className="bg-violet-700 hover:bg-violet-600 disabled:opacity-50 disabled:bg-neutral-800 disabled:text-neutral-500 text-white px-4 py-2 rounded font-semibold transition shrink-0 text-sm"
          >
            🖥 Preview
          </button>

          {/* ZIP Export */}
          <button
            onClick={handleExportZip}
            disabled={!sessionId}
            className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 disabled:bg-neutral-800 disabled:text-neutral-500 text-white px-4 py-2 rounded font-semibold transition shrink-0 text-sm"
          >
            ZIP
          </button>
        </div>
      </div>

      {/* 모드 표시 (세션 진행 중) */}
      {sessionId && (
        <div className="mt-2 flex items-center gap-2 text-[11px] text-neutral-500">
          <span>모드:</span>
          <span className={generationMode === 'code' ? 'text-emerald-400 font-bold' : 'text-blue-400 font-bold'}>
            {generationMode === 'code' ? '코드 생성' : '문서'}
          </span>
          {isRunningAll && <span className="text-amber-400 animate-pulse ml-2">● 자동 실행 중</span>}
        </div>
      )}
    </header>
  );
}
