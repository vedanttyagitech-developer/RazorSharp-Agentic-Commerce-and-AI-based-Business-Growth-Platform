import {MotionPolicy} from '@/components/continuity';
import { KeepRouterExports } from '@/components/router-exports';
import type { Metadata } from 'next';
import { Geist, Geist_Mono } from 'next/font/google';
import './globals.css';
import './motion.css';
import './voice-native.css';
import './identity.css';
import './continuity.css';
import './reserve.css';
import './discovery.css';
import './visual-system.css';
import './command-experience.css';
import './reserve-india.css';
import './product-polish.css';
import './platform-highlights.css';
import './impact-deck.css';
import './trust-boundaries.css';
import './transaction-kernel.css';
import './composer-prompts.css';

const geistSans = Geist({
  variable: '--font-geist-sans',
  subsets: ['latin'],
});

const geistMono = Geist_Mono({
  variable: '--font-geist-mono',
  subsets: ['latin'],
});

export const metadata: Metadata = {
  title: 'RazorSharp Platform — Meet Razor AI',
  description: 'Explore RazorSharp, powered by Razor AI: voice shopping, Merchant Command, transaction controls and clearly labelled connected and preview capabilities.',
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        <MotionPolicy/><KeepRouterExports/>{children}
      </body>
    </html>
  );
}
