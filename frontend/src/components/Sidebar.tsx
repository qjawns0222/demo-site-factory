"use client";

import { useRef, useEffect, useCallback } from 'react';
import { useWorkflowStore } from '../store/useWorkflowStore';
import toast from 'react-hot-toast';

const MAX_RETRIES = 3;
const RETRY_DELAYS = [2000, 4000, 8000];

export function Sidebar() {
  const {
    steps, selectedStepId, setSelectedStepId,
    isStreaming, isSynthesizing, sessionId,
    updateStep, setIsStreaming,
    pendingStepId, setPendingStepId,
    isRunningAll, setIsRunningAll,
  } = useWorkflowStore();
  const eventSourceRef = useRef<EventSource | null>(null);
  const retryCountRef = useRef<number>(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

  useEffect(() => {
    return () => {
      if (eventSourceRef.current) eventSourceRef.current.close();
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
    };
  }, []);

  const connectStream = useCallback((stepId: number, isRetry = false) => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }

    const currentSessionId = useWorkflowStore.getState().sessionId;
    if (!currentSessionId) return;

    if (!isRetry) {
      retryCountRef.current = 0;
      setSelectedStepId(stepId);
      setIsStreaming(true);
      updateStep(stepId, { status: 'WORKING', content: '' });
    }

    const { generationMode } = useWorkflowStore.getState();
    const es = new EventSource(
      `${API_URL}/api/stream_step/${currentSessionId}/${stepId}?mode=${generationMode}`
    );
    eventSourceRef.current = es;

    es.addEventListener('chunk', (e) => {
      const data = JSON.parse(e.data);
      updateStep(stepId, {
        content: (useWorkflowStore.getState().steps.find(s => s.id === stepId)?.content || '') + data.text
      });
    });

    es.addEventListener('completed', (e) => {
      const data = JSON.parse(e.data);
      updateStep(stepId, { status: 'DONE', content: data.content });
    });

    es.addEventListener('finished', async () => {
      es.close();
      retryCountRef.current = 0;
      setIsStreaming(false);
      toast.success(`${stepId}단계 스트리밍 완료`);

      // Run All 모드: 자동으로 synthesize 후 다음 단계 트리거
      if (useWorkflowStore.getState().isRunningAll) {
        const state = useWorkflowStore.getState();
        const content = state.steps.find(s => s.id === stepId)?.content || '';
        const nextStep = state.steps.find(s => s.id === stepId + 1);

        // synthesize로 컨텍스트 저장
        try {
          await fetch(`${API_URL}/api/step/${state.sessionId}/${stepId}/synthesize`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content }),
          });
        } catch {
          // synthesize 실패해도 계속 진행 (컨텍스트 없이 다음 단계 실행)
          toast(`${stepId}단계 컨텍스트 저장 실패 — 다음 단계 계속 진행`, { icon: '⚠️' });
        }

        if (nextStep) {
          setPendingStepId(nextStep.id);
        } else {
          // 마지막 단계 완료
          setIsRunningAll(false);
          toast.success('모든 단계 자동 완료!');
        }
      }
    });

    es.addEventListener('error', (e) => {
      es.close();

      let serverMsg = '';
      try {
        const msg = JSON.parse((e as MessageEvent).data);
        serverMsg = msg.error || '';
      } catch {
        // connection-level error
      }

      if (serverMsg) {
        setIsStreaming(false);
        if (useWorkflowStore.getState().isRunningAll) {
          setIsRunningAll(false);
          toast.error(`Run All 중단 — ${stepId}단계 오류: ${serverMsg}`);
        } else {
          toast.error(`스트리밍 오류: ${serverMsg}`);
        }
        updateStep(stepId, { status: 'PENDING' });
        return;
      }

      if (retryCountRef.current < MAX_RETRIES) {
        const delay = RETRY_DELAYS[retryCountRef.current];
        retryCountRef.current += 1;
        toast(`연결 끊김 — ${delay / 1000}초 후 재연결 (${retryCountRef.current}/${MAX_RETRIES})`, { icon: '🔄' });
        retryTimerRef.current = setTimeout(() => connectStream(stepId, true), delay);
      } else {
        setIsStreaming(false);
        if (useWorkflowStore.getState().isRunningAll) {
          setIsRunningAll(false);
          toast.error(`Run All 중단 — ${stepId}단계 재연결 실패`);
        } else {
          toast.error('재연결 실패 — 스텝을 다시 클릭해 주세요.');
        }
        updateStep(stepId, { status: 'PENDING' });
        retryCountRef.current = 0;
      }
    });
  }, [API_URL, setIsStreaming, setIsRunningAll, setSelectedStepId, updateStep]);

  // pendingStepId 변화 감지 → 스트림 실행
  useEffect(() => {
    if (pendingStepId === null) return;
    const currentSessionId = useWorkflowStore.getState().sessionId;
    if (!currentSessionId) return;
    setPendingStepId(null);
    connectStream(pendingStepId);
  }, [pendingStepId, connectStream, setPendingStepId]);

  const triggerStep = (stepId: number) => {
    if (!sessionId) {
      toast.error("먼저 Boot System 버튼을 눌러 세션을 할당받으세요.");
      return;
    }
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    connectStream(stepId);
  };

  return (
    <aside className="w-80 border-r border-neutral-800 bg-neutral-900/50 flex flex-col overflow-y-auto shrink-0">
      <div className="p-4 uppercase text-xs font-bold text-neutral-500 tracking-wider flex justify-between items-center border-b border-neutral-800/50">
        <span>Agent Directed Graph</span>
        {sessionId && (
          <span className="flex items-center gap-2">
            {isRunningAll && <span className="text-amber-400 text-[10px] animate-pulse">AUTO</span>}
            <span className="text-rose-500 animate-pulse text-[10px]">LIVE</span>
          </span>
        )}
      </div>
      <div className="flex flex-col p-2 space-y-1">
        {steps.length === 0 && (
          <div className="text-center text-neutral-600 text-sm mt-10">상단에서 세션을 시작하세요</div>
        )}
        {steps.map(step => (
          <button
            key={step.id}
            onClick={() => triggerStep(step.id)}
            disabled={isStreaming || isSynthesizing || !sessionId || isRunningAll}
            className={`text-left text-sm px-3 py-3 rounded-lg flex items-start gap-3 transition
              ${selectedStepId === step.id ? 'bg-neutral-800 text-white ring-1 ring-neutral-700' : 'text-neutral-400 hover:bg-neutral-800/50'}
              ${(isStreaming || isSynthesizing || isRunningAll) && selectedStepId !== step.id ? 'opacity-50 cursor-not-allowed' : ''}
            `}
          >
            <div className="mt-0.5 shrink-0">
              {step.status === 'DONE' && <div className="w-2 h-2 rounded-full bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.6)]" />}
              {step.status === 'WORKING' && <div className="w-2 h-2 rounded-full bg-blue-500 animate-pulse shadow-[0_0_8px_rgba(59,130,246,0.6)]" />}
              {step.status === 'PENDING' && <div className="w-2 h-2 rounded-full bg-neutral-700" />}
            </div>
            <span className="leading-snug">{step.name}</span>
          </button>
        ))}
      </div>
    </aside>
  );
}
