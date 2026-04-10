import { create } from 'zustand';

export type StepState = {
  id: number;
  name: string;
  status: 'PENDING' | 'WORKING' | 'DONE' | 'ERROR';
  content?: string;
};

export type StatusBanner = {
  type: 'success' | 'error' | 'info';
  message: string;
  stepId?: number;
};

interface WorkflowState {
  domain: string;
  sessionId: string;
  steps: StepState[];
  selectedStepId: number | null;
  isStreaming: boolean;
  isSynthesizing: boolean;
  pendingStepId: number | null;
  isRunningAll: boolean;
  statusBanners: StatusBanner[];
  synthesizedContent: Map<number, string>;

  setDomain: (domain: string) => void;
  setSessionId: (id: string) => void;
  setSteps: (steps: StepState[]) => void;
  updateStep: (id: number, data: Partial<StepState>) => void;
  setSelectedStepId: (id: number | null) => void;
  setIsStreaming: (val: boolean) => void;
  setIsSynthesizing: (val: boolean) => void;
  setPendingStepId: (id: number | null) => void;
  setIsRunningAll: (val: boolean) => void;
  addBanner: (banner: StatusBanner) => void;
  dismissBanner: (index: number) => void;
  clearBanners: () => void;
  markSynthesized: (id: number, content: string) => void;
  resetAll: () => void;
}

export const useWorkflowStore = create<WorkflowState>((set) => ({
  domain: '',
  sessionId: '',
  steps: [],
  selectedStepId: null,
  isStreaming: false,
  isSynthesizing: false,
  pendingStepId: null,
  isRunningAll: false,
  statusBanners: [],
  synthesizedContent: new Map<number, string>(),

  setDomain: (domain) => set({ domain }),
  setSessionId: (id) => set({ sessionId: id }),
  setSteps: (steps) => set({ steps }),
  updateStep: (id, data) =>
    set((state) => ({
      steps: state.steps.map(s => s.id === id ? { ...s, ...data } : s)
    })),
  setSelectedStepId: (id) => set({ selectedStepId: id }),
  setIsStreaming: (val) => set({ isStreaming: val }),
  setIsSynthesizing: (val) => set({ isSynthesizing: val }),
  setPendingStepId: (id) => set({ pendingStepId: id }),
  setIsRunningAll: (val) => set({ isRunningAll: val }),
  addBanner: (banner) => set((state) => ({ statusBanners: [...state.statusBanners, banner] })),
  dismissBanner: (index) => set((state) => ({ statusBanners: state.statusBanners.filter((_, i) => i !== index) })),
  clearBanners: () => set({ statusBanners: [] }),
  markSynthesized: (id, content) => set((state) => {
    const next = new Map(state.synthesizedContent);
    next.set(id, content);
    return { synthesizedContent: next };
  }),
  resetAll: () => set({
    steps: [],
    selectedStepId: null,
    sessionId: '',
    domain: '',
    pendingStepId: null,
    isRunningAll: false,
    statusBanners: [],
    synthesizedContent: new Map<number, string>(),
  }),
}));

// store 외부 유틸: synthesize 후 내용이 변경됐는지 확인
export function isSynthesizedCurrent(stepId: number): boolean {
  const state = useWorkflowStore.getState();
  const savedContent = state.synthesizedContent.get(stepId);
  if (savedContent === undefined) return false;
  const currentContent = state.steps.find((s: StepState) => s.id === stepId)?.content ?? '';
  return savedContent === currentContent;
}
