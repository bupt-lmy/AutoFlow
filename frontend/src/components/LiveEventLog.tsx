import { useEffect, useRef } from 'react';
import { Tag } from 'antd';
import type { WsEvent } from '../api/ws';

const typeColors: Record<string, string> = {
  routing: 'green',
  tool_call: 'orange',
  tool_error: 'red',
  llm_summary: 'blue',
  agent_message: 'purple',
  'workflow.started': 'cyan',
  'workflow.completed': 'green',
  'workflow.failed': 'red',
  'node.task_assigned': 'blue',
  'node.task_started': 'gold',
  'node.result_ready': 'green',
  'node.error': 'red',
};

interface Props {
  events: WsEvent[];
}

export default function LiveEventLog({ events }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [events]);

  return (
    <div style={{
      background: '#1a1a2e',
      borderRadius: 8,
      padding: 16,
      fontFamily: 'monospace',
      fontSize: 12,
      color: '#e0e0e0',
      maxHeight: 400,
      overflowY: 'auto',
    }}>
      {events.length === 0 && (
        <div style={{ color: '#666' }}>等待执行事件...</div>
      )}
      {events.map((evt, i) => (
        <div key={i} style={{ marginBottom: 4 }}>
          <span style={{ color: '#666', marginRight: 8 }}>
            {evt.timestamp ? new Date(evt.timestamp).toLocaleTimeString() : '--:--:--'}
          </span>
          <Tag color={typeColors[evt.type] || 'default'} style={{ fontSize: 11 }}>
            {evt.type}
          </Tag>
          <span>{JSON.stringify(evt.data)}</span>
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
