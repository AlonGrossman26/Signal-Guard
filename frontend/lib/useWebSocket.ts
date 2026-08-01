"use client";

import { useEffect, useRef, useState } from "react";
import { wsUrl } from "./api";
import type { WsEvent } from "./types";

export type WsStatus = "connecting" | "open" | "closed";

// Subscribes to /ws and hands each decision/order/position/equity event to
// `onEvent`. Reconnects automatically with a short backoff — a dashboard that
// silently stops updating is worse than one that briefly shows "reconnecting".
export function useWebSocket(onEvent: (event: WsEvent) => void): WsStatus {
  const [status, setStatus] = useState<WsStatus>("connecting");
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let closed = false;

    const connect = () => {
      setStatus("connecting");
      socket = new WebSocket(wsUrl());

      socket.onopen = () => setStatus("open");
      socket.onmessage = (msg) => {
        try {
          const event = JSON.parse(msg.data as string) as WsEvent;
          if (event.type === "heartbeat" || event.type === "connected") return;
          onEventRef.current(event);
        } catch {
          // Ignore anything that is not a JSON event.
        }
      };
      socket.onclose = () => {
        setStatus("closed");
        if (!closed) {
          reconnectTimer = setTimeout(connect, 2000);
        }
      };
      socket.onerror = () => socket?.close();
    };

    connect();

    return () => {
      closed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, []);

  return status;
}
