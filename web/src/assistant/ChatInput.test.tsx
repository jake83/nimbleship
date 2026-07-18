import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { ChatInput } from './ChatInput'

describe('ChatInput', () => {
  it('submits on Enter and clears the field', async () => {
    const onSubmit = vi.fn()
    render(<ChatInput onSubmit={onSubmit} />)

    const input = screen.getByLabelText(/message the assistant/i)
    await userEvent.type(input, 'why did it ship?{Enter}')

    expect(onSubmit).toHaveBeenCalledWith('why did it ship?')
    expect(input).toHaveValue('')
  })

  it('inserts a newline on Shift+Enter without submitting', async () => {
    const onSubmit = vi.fn()
    render(<ChatInput onSubmit={onSubmit} />)

    const input = screen.getByLabelText(/message the assistant/i)
    await userEvent.type(input, 'line one{Shift>}{Enter}{/Shift}line two')

    expect(onSubmit).not.toHaveBeenCalled()
    expect(input).toHaveValue('line one\nline two')
  })

  it('does not submit a blank message', async () => {
    const onSubmit = vi.fn()
    render(<ChatInput onSubmit={onSubmit} />)

    await userEvent.type(screen.getByLabelText(/message the assistant/i), '   {Enter}')

    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('does not submit while an IME composition is active', () => {
    const onSubmit = vi.fn()
    render(<ChatInput onSubmit={onSubmit} />)

    const input = screen.getByLabelText(/message the assistant/i)
    fireEvent.change(input, { target: { value: 'composing' } })
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true })

    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('does not submit when disabled', async () => {
    const onSubmit = vi.fn()
    render(<ChatInput onSubmit={onSubmit} disabled />)

    await userEvent.type(
      screen.getByLabelText(/message the assistant/i),
      'hello{Enter}',
    )

    expect(onSubmit).not.toHaveBeenCalled()
  })
})
