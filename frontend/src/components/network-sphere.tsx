import { useEffect, useRef } from "react";

interface NetworkSphereProps {
  /** Rendered width/height in CSS pixels (the sphere is square + circular). */
  size?: number;
  /** Extra class names applied to the canvas element. */
  className?: string;
  /** Number of nodes distributed on the sphere surface. */
  nodeCount?: number;
  /** Color of the dots and connecting lines. */
  color?: string;
  /** Circular background fill behind the sphere. */
  background?: string;
}

interface Pt3 {
  // unit direction on the sphere (length 1)
  ux: number;
  uy: number;
  uz: number;
  // base radius offset so the surface is irregular/jagged (not a smooth ball)
  baseR: number;
  // per-node radial oscillation so individual nodes push out / pull in
  phase: number;
  amp: number;
  speed: number;
}

/**
 * NetworkSphere — a live, animated "plexus" sphere rendered on <canvas>.
 *
 * Points are distributed on the surface of a sphere (Fibonacci spiral),
 * continuously rotated around the Y/X axes, projected to 2D with perspective,
 * and connected with lines when they are close in 3D space. Depth controls
 * opacity so the far side of the sphere fades — matching the reference image.
 */
export function NetworkSphere({
  size = 56,
  className,
  nodeCount = 90,
  color = "255, 255, 255",
  background = "#000000",
}: NetworkSphereProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    ctx.scale(dpr, dpr);

    const cx = size / 2;
    const cy = size / 2;
    const radius = size * 0.42;

    // ── Distribute nodes on a sphere via the Fibonacci spiral ──
    // Each node keeps a *unit direction* plus an irregular base radius, so the
    // surface is jagged (not a smooth ball) like the reference image.
    const pts: Pt3[] = [];
    const golden = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < nodeCount; i++) {
      const y = 1 - (i / (nodeCount - 1)) * 2; // -1..1
      const r = Math.sqrt(1 - y * y);
      const theta = golden * i;
      pts.push({
        ux: Math.cos(theta) * r,
        uy: y,
        uz: Math.sin(theta) * r,
        baseR: 0.78 + Math.random() * 0.22, // irregular surface (0.78..1.0)
        phase: Math.random() * Math.PI * 2,
        amp: 0.1 + Math.random() * 0.18, // each node pushes out / pulls in
        speed: 0.8 + Math.random() * 1.4,
      });
    }

    // ── Precompute a stable triangulated mesh ──────────────────────────
    // Connect every node to its K nearest neighbours (by angular distance on
    // the sphere). This guarantees EVERY node is wired into the web, giving the
    // consistent triangulated framework from the reference image.
    const K = 6;
    const edgeSet = new Set<number>();
    const edges: [number, number][] = [];
    for (let i = 0; i < pts.length; i++) {
      const a = pts[i];
      const dists: { j: number; d: number }[] = [];
      for (let j = 0; j < pts.length; j++) {
        if (j === i) continue;
        const b = pts[j];
        const dx = a.ux - b.ux;
        const dy = a.uy - b.uy;
        const dz = a.uz - b.uz;
        dists.push({ j, d: dx * dx + dy * dy + dz * dz });
      }
      dists.sort((p, q) => p.d - q.d);
      for (let k = 0; k < Math.min(K, dists.length); k++) {
        const j = dists[k].j;
        const key = i < j ? i * pts.length + j : j * pts.length + i;
        if (!edgeSet.has(key)) {
          edgeSet.add(key);
          edges.push(i < j ? [i, j] : [j, i]);
        }
      }
    }

    let rotX = 0.35;
    let rotY = 0;
    let raf = 0;
    let last = performance.now();

    const projected = new Array(pts.length).fill(null) as ({
      sx: number;
      sy: number;
      depth: number; // 0 (far) .. 1 (near)
      scale: number;
    } | null)[];

    function frame(now: number) {
      const dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      rotY += dt * 0.5;
      rotX += dt * 0.12;

      const cosY = Math.cos(rotY);
      const sinY = Math.sin(rotY);
      const cosX = Math.cos(rotX * 0.5);
      const sinX = Math.sin(rotX * 0.5);
      const t = now / 1000;

      ctx!.clearRect(0, 0, size, size);

      // Circular dark backdrop so the white mesh pops.
      ctx!.save();
      ctx!.beginPath();
      ctx!.arc(cx, cy, size / 2, 0, Math.PI * 2);
      ctx!.closePath();
      ctx!.fillStyle = background;
      ctx!.fill();
      ctx!.clip();

      // Global breathing: the whole sphere expands and contracts.
      const breathe = 1 + Math.sin(t * 0.9) * 0.14;

      // Project every node.
      for (let i = 0; i < pts.length; i++) {
        const p = pts[i];
        // Smooth global breathing only — no per-node jitter/shaking.
        const rNode = p.baseR * breathe;
        let x = p.ux * rNode;
        let y = p.uy * rNode;
        let z = p.uz * rNode;

        // Rotate around Y axis.
        let dx = x * cosY - z * sinY;
        let dz = x * sinY + z * cosY;
        x = dx;
        z = dz;
        // Rotate around X axis.
        const dy = y * cosX - z * sinX;
        dz = y * sinX + z * cosX;
        y = dy;
        z = dz;

        // Perspective projection.
        const perspective = 1.8;
        const scale = perspective / (perspective - z);
        projected[i] = {
          sx: cx + x * radius * scale,
          sy: cy + y * radius * scale,
          depth: (z + 1) / 2,
          scale,
        };
      }

      // Draw the fixed triangulated mesh — every precomputed edge.
      ctx!.lineWidth = 0.8;
      for (let e = 0; e < edges.length; e++) {
        const a = projected[edges[e][0]];
        const b = projected[edges[e][1]];
        if (!a || !b) continue;
        const depth = (a.depth + b.depth) / 2;
        const alpha = 0.25 + depth * 0.55;
        ctx!.strokeStyle = `rgba(${color}, ${alpha.toFixed(3)})`;
        ctx!.beginPath();
        ctx!.moveTo(a.sx, a.sy);
        ctx!.lineTo(b.sx, b.sy);
        ctx!.stroke();
      }

      // Draw nodes on top.
      for (let i = 0; i < pts.length; i++) {
        const a = projected[i];
        if (!a) continue;
        const rDot = (0.15 + a.depth * 0.35) * (size / 56);
        const alpha = 0.35 + a.depth * 0.65;
        ctx!.fillStyle = `rgba(${color}, ${alpha.toFixed(3)})`;
        ctx!.beginPath();
        ctx!.arc(a.sx, a.sy, rDot, 0, Math.PI * 2);
        ctx!.fill();
      }

      ctx!.restore();
      raf = requestAnimationFrame(frame);
    }

    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [size, nodeCount, color, background]);

  return (
    <canvas
      ref={canvasRef}
      className={className}
      style={{ width: size, height: size, display: "block" }}
    />
  );
}
