import { Link, NavLink, Outlet, Route, Routes } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { cn } from '@/lib/utils'
import { DraftEditorPage } from '@/rulebook/DraftEditorPage'
import { VersionPage } from '@/rulebook/VersionPage'
import { VersionsPage } from '@/rulebook/VersionsPage'

function Layout() {
  return (
    <div className="flex min-h-screen flex-col bg-background">
      <header className="border-b">
        <nav className="mx-auto flex w-full max-w-5xl items-center gap-6 px-6 py-3">
          <Link to="/" className="font-heading font-semibold tracking-tight">
            NimbleShip
          </Link>
          <NavLink
            to="/rulebook"
            className={({ isActive }) =>
              cn(
                'text-sm text-muted-foreground transition-colors hover:text-foreground',
                isActive && 'font-medium text-foreground',
              )
            }
          >
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
  return (
    <div className="flex justify-center pt-16">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle>
            <h1 className="text-2xl font-semibold tracking-tight">NimbleShip</h1>
          </CardTitle>
          <CardDescription>
            Carrier management, rebuilt. Successor to the 3PL proxy.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex gap-2">
          <Button nativeButton={false} render={<Link to="/rulebook" />}>
            Rulebook
          </Button>
          <Button
            nativeButton={false}
            variant="outline"
            render={<a href="/api/docs" />}
          >
            API documentation
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}

function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Home />} />
        <Route path="/rulebook" element={<VersionsPage />} />
        <Route path="/rulebook/versions/:version" element={<VersionPage />} />
        <Route path="/rulebook/drafts/new" element={<DraftEditorPage />} />
      </Route>
    </Routes>
  )
}

export default App
