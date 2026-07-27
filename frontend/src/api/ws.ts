import { useEffect, useRef, useState, useCallback } from 'react';

interface WsEventData {
  [key: string]: unknown;
  error?: unknown;
  payload?: WsEventData;
  failures?: unknown;
  node_id?: string;
  agent_id?: string;
  supervisor_agent_id?: string;
  input?: unknown;
  arguments?: { next_attempt?: number };
  terminal_candidate?: boolean;
  next_agents?: string[];
  outcome?: string;
  task?: unknown;
  content?: unknown;
}

export interface WsEvent {
  type: string;
  workflow_id?: string;
  run_id?: string;
  timestamp?: string;
  data?: WsEventData;
}

export function useWebSocket(runId: string | null) {
  const [events, setEvents] = useState<WsEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!runId) return;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${window.location.host}/ws/events?run_id=${runId}`;
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onmessage = (e) => {
      try {
        const event: WsEvent = JSON.parse(e.data);
        setEvents(prev => [...prev, event]);
      } catch {
        // Ignore non-JSON keepalive or malformed messages.
      }
    };

    // Keep alive ping
    const ping = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send('ping');
    }, 30000);

    return () => {
      clearInterval(ping);
      ws.close();
    };
  }, [runId]);

  const clearEvents = useCallback(() => setEvents([]), []);
  return { events, connected, clearEvents };
}
