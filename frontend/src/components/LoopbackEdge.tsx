import { BaseEdge } from 'reactflow';
import type { EdgeProps } from 'reactflow';

export default function LoopbackEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  label,
  markerEnd,
  style,
  interactionWidth,
}: EdgeProps) {
  const margin = 72;
  const rightX = Math.max(sourceX, targetX) + margin;
  const leftX = Math.min(sourceX, targetX) - margin;
  const outsideY = sourceY >= targetY
    ? Math.max(sourceY, targetY) + margin
    : Math.min(sourceY, targetY) - margin;
  const path = [
    `M ${sourceX} ${sourceY}`,
    `L ${rightX} ${sourceY}`,
    `L ${rightX} ${outsideY}`,
    `L ${leftX} ${outsideY}`,
    `L ${leftX} ${targetY}`,
    `L ${targetX} ${targetY}`,
  ].join(' ');

  return (
    <BaseEdge
      id={id}
      path={path}
      label={label}
      labelX={(leftX + rightX) / 2}
      labelY={outsideY}
      markerEnd={markerEnd}
      interactionWidth={interactionWidth}
      style={{ strokeLinejoin: 'round', strokeLinecap: 'round', ...style }}
    />
  );
}
