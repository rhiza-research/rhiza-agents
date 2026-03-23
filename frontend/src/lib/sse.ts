export interface SSEEvent {
    type: string;
    data: any;
}

/**
 * Parse SSE event stream buffer into structured events.
 */
export function parseSSEEvents(buffer: string): { parsed: SSEEvent[]; remaining: string } {
    const parsed: SSEEvent[] = [];
    const lines = buffer.split('\n');
    let remaining = '';
    let currentEvent: SSEEvent | null = null;

    for (const line of lines) {
        if (line.startsWith('event: ')) {
            currentEvent = { type: line.slice(7).trim(), data: '' };
        } else if (line.startsWith('data: ') && currentEvent) {
            currentEvent.data = line.slice(6);
        } else if (line === '' && currentEvent) {
            try {
                currentEvent.data = JSON.parse(currentEvent.data);
            } catch { /* keep as string */ }
            parsed.push(currentEvent);
            currentEvent = null;
        }
    }

    if (currentEvent) {
        const dataStr = typeof currentEvent.data === 'string'
            ? currentEvent.data
            : JSON.stringify(currentEvent.data);
        remaining = `event: ${currentEvent.type}\ndata: ${dataStr}\n`;
    }

    return { parsed, remaining };
}
