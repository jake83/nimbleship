import { ArrowUp } from 'lucide-react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'

interface ChatInputProps {
  onSubmit: (text: string) => void
  disabled?: boolean
  pending?: boolean
  placeholder?: string
  autoFocus?: boolean
  ariaLabel?: string
}

/** The assistant's input: a textarea that grows with content up to ~8 lines then
 * scrolls (native field-sizing plus a max height). Enter sends, Shift+Enter is a
 * newline. Submitting a blank message is a no-op. */
export function ChatInput({
  onSubmit,
  disabled = false,
  pending = false,
  placeholder = 'Ask why an order shipped the way it did…',
  autoFocus = false,
  ariaLabel = 'Message the assistant',
}: ChatInputProps) {
  const [text, setText] = useState('')
  const canSend = text.trim().length > 0 && !disabled && !pending

  function submit() {
    if (!canSend) return
    onSubmit(text.trim())
    setText('')
  }

  return (
    <div className="relative">
      <Textarea
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          // isComposing: an IME's Enter confirms a candidate, it doesn't send -
          // otherwise a half-composed message submits mid-composition.
          if (
            event.key === 'Enter' &&
            !event.shiftKey &&
            !event.nativeEvent.isComposing
          ) {
            event.preventDefault()
            submit()
          }
        }}
        disabled={disabled}
        placeholder={placeholder}
        autoFocus={autoFocus}
        rows={1}
        aria-label={ariaLabel}
        className="max-h-52 resize-none overflow-y-auto pr-12"
      />
      <Button
        size="icon"
        className="absolute right-2 bottom-2"
        disabled={!canSend}
        onClick={submit}
        aria-label="Send"
      >
        <ArrowUp className="size-4" />
      </Button>
    </div>
  )
}
