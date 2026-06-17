import { useCallback, useEffect, useRef } from "react";
import type { UIMessage } from "ai";
import {
  chatHistoryKey,
  clearMessages,
  loadMessages,
  nextChatHistoryAction,
  purgeForeignKeys,
  saveMessages,
} from "./chatHistory";

/** Wires the pure chat-history policy (nextChatHistoryAction) into the chat
 *  lifecycle. All persistence decisions live in chatHistory.ts and are unit
 *  tested there; this hook is the thin, side-effecting applicator.
 *
 *  One effect owns restore/adopt/persist, which makes the restore→persist
 *  ordering race structurally impossible: on the pass that claims a user we
 *  mark them restored and return, so the transitional transcript is never
 *  written under their key. On first sight of a user we also purge every other
 *  user's blob from this device (shared-browser privacy). */
export function useChatHistory(opts: {
  storage: Storage;
  meEmail: string | null;
  role: string | null;
  messages: UIMessage[];
  status: string;
  setMessages: (messages: UIMessage[]) => void;
}): { newChat: () => void; clearStored: () => void } {
  const { storage, meEmail, role, messages, status, setMessages } = opts;
  const restoredFor = useRef<string | null>(null);

  useEffect(() => {
    const action = nextChatHistoryAction(restoredFor.current, meEmail, status, messages.length > 0);
    if (action.kind === "restore" || action.kind === "adopt") {
      restoredFor.current = meEmail; // mark BEFORE setMessages so the next pass persists, not this one
      purgeForeignKeys(storage, action.key); // drop other users' transcripts on this device
      if (action.kind === "restore") {
        // no draft on screen: load this user's own history (incl. [] — never another user's)
        setMessages(loadMessages(storage, action.key, { role }));
      }
      // adopt: a transcript is already on screen (e.g. a message sent before /me
      // resolved) — keep it; it persists under the user's key on the next pass.
    } else if (action.kind === "persist") {
      saveMessages(storage, action.key, messages, { role });
    }
  }, [storage, meEmail, role, messages, status, setMessages]);

  const newChat = useCallback(() => {
    clearMessages(storage, chatHistoryKey(meEmail));
    setMessages([]);
  }, [storage, meEmail, setMessages]);

  // Wipe the current user's transcript on explicit sign-out (shared/kiosk
  // browser): localStorage outlives the server session otherwise.
  const clearStored = useCallback(() => {
    clearMessages(storage, chatHistoryKey(meEmail));
  }, [storage, meEmail]);

  return { newChat, clearStored };
}
