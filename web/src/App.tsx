import { useEffect, useState } from 'react'
import {
  Link,
  NavLink,
  Outlet,
  Route,
  Routes,
  useNavigate,
} from 'react-router-dom'

import { fetchAssistantStatus } from '@/assistant/api'
import { AssistantPage } from '@/assistant/AssistantPage'
import { ChatInput } from '@/assistant/ChatInput'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { BuilderPage } from '@/rulebook/BuilderPage'
import { DraftEditorPage } from '@/rulebook/DraftEditorPage'
import { VersionPage } from '@/rulebook/VersionPage'
import { VersionsPage } from '@/rulebook/VersionsPage'

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    'text-sm text-muted-foreground transition-colors hover:text-foreground',
    isActive && 'font-medium text-foreground',
  )

function Layout() {
  return (
    <div className="flex min-h-screen flex-col bg-background">
      <header className="border-b">
        <nav className="mx-auto flex w-full max-w-5xl items-center gap-6 px-6 py-3">
          <Link to="/" className="font-heading font-semibold tracking-tight">
            NimbleShip
          </Link>
          <NavLink to="/assistant" className={navLinkClass}>
            Assistant
          </NavLink>
          <NavLink to="/rulebook" className={navLinkClass}>
            Rulebook
          </NavLink>
        </nav>
      </header>
      <main className="mx-auto w-full max-w-5xl flex-1 px-6 py-8">
        <Outlet />
      </main>
    </div>
  )
}

function Home() {
  const navigate = useNavigate()
  const [configured, setConfigured] = useState<boolean | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchAssistantStatus()
      .then((status) => {
        if (!cancelled) setConfigured(status.configured)
      })
      .catch(() => {
        if (!cancelled) setConfigured(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-6 pt-12">
      <div className="text-center">
        <h1 className="text-2xl font-semibold tracking-tight">NimbleShip</h1>
        <p className="mt-1 text-muted-foreground">
          Ask why an order shipped the way it did.
        </p>
      </div>
      {configured === false ? (
        <p className="text-center text-sm text-muted-foreground">
          The assistant isn&apos;t configured on this install.
        </p>
      ) : (
        <ChatInput
          onSubmit={(text) =>
            navigate('/assistant', { state: { initial: text } })
          }
          disabled={configured !== true}
        />
      )}
      <div className="flex justify-center gap-2">
        <Button
          nativeButton={false}
          variant="outline"
          render={<Link to="/rulebook" />}
        >
          Rulebook
        </Button>
        <Button
          nativeButton={false}
          variant="outline"
          render={<a href="/api/docs" />}
        >
          API documentation
        </Button>
      </div>
    </div>
  )
}

function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Home />} />
        <Route path="/assistant" element={<AssistantPage />} />
        <Route path="/rulebook" element={<VersionsPage />} />
        <Route path="/rulebook/builder" element={<BuilderPage />} />
        <Route path="/rulebook/versions/:version" element={<VersionPage />} />
        <Route path="/rulebook/drafts/new" element={<DraftEditorPage />} />
      </Route>
    </Routes>
  )
}

export default App
