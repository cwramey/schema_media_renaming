import { clsx, type ClassValue } from 'clsx';

/** className combiner (clsx). */
export function cn(...inputs: ClassValue[]): string {
  return clsx(inputs);
}
