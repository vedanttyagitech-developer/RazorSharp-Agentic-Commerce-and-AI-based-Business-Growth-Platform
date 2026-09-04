import Link from "next/link";

export default function NotFound() {
  return (
    <div className="space-y-2">
      <h1 className="text-2xl font-semibold">Not found</h1>
      <p className="text-sm text-muted">That page does not exist. <Link href="/" className="underline">Back to the store</Link>.</p>
    </div>
  );
}
