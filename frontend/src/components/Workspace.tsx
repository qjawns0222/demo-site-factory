"use client";

import { useState, useRef, useEffect } from 'react';
import { useWorkflowStore } from '../store/useWorkflowStore';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import toast from 'react-hot-toast';

export function Workspace() {
  const {
    steps, selectedStepId, isStreaming, isSynthesizing,
    updateStep, setIsSynthesizing, sessionId,
    setPendingStepId, isRunningAll,
  } = useWorkflowStore();
  const [isEditing, setIsEditing] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const selectedStep = steps.find(s => s.id === selectedStepId);
  const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

  useEffect(() => {
    if (isStreaming && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [selectedStep?.content, isStreaming]);

  const approveAndNext = async () => {
    if (!selectedStep || !sessionId) return;

    setIsSynthesizing(true);
    const content = selectedStep.content || '';

    try {
      const res = await fetch(`${API_URL}/api/step/${sessionId}/${selectedStep.id}/synthesize`, {
        method: 'POST',
        body: JSON.stringify({ content }),
        headers: { 'Content-Type': 'application/json' }
      });

      if (!res.ok) {
        const errData = await res.json();
        throw new Error(errData.detail || '승인에 실패했습니다.');
      }

      toast.success('컨텍스트 합성 완료 — 다음 단계를 시작합니다.');

      const currentSteps = useWorkflowStore.getState().steps;
      const nextStep = currentSteps.find(s => s.id === (selectedStep.id + 1));
      if (nextStep) {
        setPendingStepId(nextStep.id);
      }
    } catch (err: any) {
      toast.error(`루프 병합 실패: ${err.message}`);
      if (isRunningAll) {
        useWorkflowStore.getState().setIsRunningAll(false);
      }
    } finally {
      setIsSynthesizing(false);
    }
  };

  if (!selectedStep) {
    return (
      <section className="flex-1 bg-neutral-950 flex flex-col items-center justify-center text-neutral-600">
        <div className="text-center font-mono animate-pulse">
          <div className="text-4xl mb-4 text-neutral-800">■</div>
          <p>SYSTEM READY.</p>
          <p className="text-sm mt-2">상단에서 세션을 시작하고 좌측 워크플로우를 클릭하십시오.</p>
        </div>
      </section>
    );
  }

  return (
    <section className="flex-1 bg-neutral-950 flex flex-col h-full overflow-hidden">
      <div className="h-full flex flex-col max-w-5xl mx-auto w-full p-6 overflow-hidden">
        <div className="mb-4 pb-4 border-b border-neutral-800 flex justify-between items-end shrink-0">
          <div>
            <span className="text-[10px] uppercase tracking-widest text-blue-500 font-bold mb-1 block">Human-in-the-Loop Gateway</span>
            <h2 className="text-2xl font-bold text-white tracking-tight">{selectedStep.name}</h2>
          </div>

          {selectedStep.status === 'DONE' && !isStreaming && !isRunningAll && (
            <div className="flex gap-2">
              <button
                onClick={() => setIsEditing(!isEditing)}
                className="px-3 py-1.5 bg-neutral-800 hover:bg-neutral-700 text-neutral-300 rounded text-sm font-semibold transition border border-neutral-700"
              >
                {isEditing ? '✔ 미리보기로 복귀' : '✍ 마크다운 직접 조작'}
              </button>
              <button
                onClick={approveAndNext}
                disabled={isSynthesizing}
                className="px-4 py-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded text-sm font-bold transition disabled:opacity-50 flex items-center gap-2"
              >
                {isSynthesizing ? <span className="animate-spin text-lg">⚙</span> : null}
                {isSynthesizing ? '컨텍스트 합성 중...' : '컨텍스트 승인 (Approve & Next)'}
              </button>
            </div>
          )}

          {isRunningAll && selectedStep.status === 'WORKING' && (
            <div className="text-amber-400 font-mono text-sm flex items-center gap-2">
              <span className="animate-spin">⚙</span>
              Run All 자동 실행 중...
            </div>
          )}
        </div>

        <div className="flex-1 flex flex-col bg-[#111] border border-neutral-800 rounded-xl shadow-2xl overflow-hidden relative">
          {isEditing ? (
            <textarea
              className="w-full h-full bg-transparent text-neutral-300 p-6 outline-none font-mono text-sm leading-relaxed resize-none custom-scrollbar"
              value={selectedStep.content}
              onChange={(e) => updateStep(selectedStep.id, { content: e.target.value })}
              spellCheck={false}
            />
          ) : (
            <div ref={scrollRef} className="flex-1 p-8 overflow-y-auto break-words relative scroll-smooth custom-scrollbar">
              {selectedStep.status === 'PENDING' && (
                <div className="text-neutral-600 italic font-mono flex items-center gap-2">선택 대기 중...</div>
              )}

              <div className="prose prose-invert prose-base max-w-none prose-p:leading-relaxed prose-pre:bg-neutral-900 prose-pre:border prose-pre:border-neutral-800 prose-headings:text-neutral-200">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {selectedStep.content || ''}
                </ReactMarkdown>
              </div>

              {isStreaming && (
                <div className="mt-8 flex items-center gap-2 text-rose-400 font-mono text-sm border-t border-neutral-800 pt-4">
                  <span className="w-2 h-4 bg-rose-500 inline-block animate-ping"></span>
                  실시간 토큰을 동기화 중입니다...
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
