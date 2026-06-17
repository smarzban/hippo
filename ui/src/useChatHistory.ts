import { useCallback, useEffect, useRef } from "react";
import type { UIMessage } from "ai";
import {
  chatHistoryKey,
  clearMessages,
  loadMessages,
  nextChatHistoryAction,
  saveMessages,
} from "./chatHistory";

/** Wires the pure chat-history policy (nextChatHistoryAction) into the chat
 *  lifecycle. All persistence decisions live in chatHistory.ts and are unit
 *  tested there; this hook is the thin, side-effecting applicator.
 *
 *  One effect owns both restore and persist, which makes the restore→persist
 *  ordering race structurally impossible: on the pass that restores a user we
 *  mark them restored and return, so the transitional (seed) transcript is
 *  never written under their key. */
export function useChatHistory(opts: {
  storage: Storage;
  meEmail: string | null;
  messages: UIMessage[];
  status: string;
  setMessages: (messages: UIMessage[]) => void;
}): { newChat: () => void; clearStored: () => void } {
  const { storage, meEmail, messages, status, setMessages } = opts;
  const restoredFor = useRef<string | null>(null);

  useEffect(() => {
    const action = nextChatHistoryAction(restoredFor.current, meEmail, status);
    if (action.kind === "restore") {
      restoredFor.current = meEmail; // mark BEFORE setMessages so the next pass persists, not this one
      const restored = loadMessages(storage, action.key);
      clearMessages(storage, action.clearKey); // discard the legacy shared "default" key
      setMessages(restored); // incl. [] — no history shows an empty chat, never another user's
    } else if (action.kind === "persist") {
      saveMessages(storage, action.key, messages);
    }
  }, [storage, meEmail, messages, status, setMessages]);

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
