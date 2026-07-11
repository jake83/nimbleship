import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'

function App() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-background">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle>
            <h1 className="text-2xl font-semibold tracking-tight">NimbleShip</h1>
          </CardTitle>
          <CardDescription>
            Carrier management, rebuilt. Successor to the 3PL proxy.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button render={<a href="/api/docs" />}>API documentation</Button>
        </CardContent>
      </Card>
    </main>
  )
}

export default App
