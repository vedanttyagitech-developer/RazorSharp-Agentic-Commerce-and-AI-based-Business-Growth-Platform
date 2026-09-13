'use client';
import { useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from './ui/dialog';
import { ProductArt } from './concept';
import { money, type Product } from '@/lib/demo';
import { Columns3 } from 'lucide-react';
export function ProductComparison({ products }: { products: Product[] }) {
  const [open, setOpen] = useState(false);
  const [ids, setIds] = useState<string[]>([]);
  const selected = ids
    .map((id) => products.find((p) => p.id === id))
    .filter((p): p is Product => !!p);
  return (
    <>
      <button
        className="comparison-open"
        disabled={products.length < 2}
        onClick={() => {
          setIds(products.slice(0, 2).map((p) => p.id));
          setOpen(true);
        }}
      >
        <Columns3 size={15} /> Compare products
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="concept-dialog comparison-sheet">
          <DialogHeader>
            <DialogTitle>Compare your finds</DialogTitle>
            <DialogDescription>
              Current catalogue values for these results. Pack prices are not
              normalized unit prices.
            </DialogDescription>
          </DialogHeader>
          <div className="comparison-columns">
            {selected.map((p, i) => (
              <section key={i}>
                <select
                  aria-label={`Comparison product ${i + 1}`}
                  value={p.id}
                  onChange={(e) =>
                    setIds((old) =>
                      old.map((id, j) => (j === i ? e.target.value : id)),
                    )
                  }
                >
                  {products.map((item) => (
                    <option
                      disabled={ids[1 - i] === item.id}
                      value={item.id}
                      key={item.id}
                    >
                      {item.name}
                    </option>
                  ))}
                </select>
                <ProductArt product={p} />
                <h3>{p.name}</h3>
                <dl>
                  <dt>Pack price</dt>
                  <dd>{money(p.price)}</dd>
                  <dt>Pack description</dt>
                  <dd>{p.unit}</dd>
                  <dt>Category</dt>
                  <dd>{p.category}</dd>
                  <dt>Availability</dt>
                  <dd>
                    {p.isListed === false
                      ? 'Unlisted'
                      : p.isAvailable === false || p.stock === 0
                        ? 'Unavailable'
                        : 'Available'}
                  </dd>
                  <dt>Recorded stock</dt>
                  <dd>{p.stock} units</dd>
                </dl>
              </section>
            ))}
          </div>
          <p className="muted">
            Final availability and price are checked again in your cart.
            Ingredients and dietary suitability are not inferred.
          </p>
        </DialogContent>
      </Dialog>
    </>
  );
}
