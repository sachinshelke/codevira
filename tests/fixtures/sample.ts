/**
 * Sample TypeScript module for testing tree-sitter parsing.
 * Demonstrates functions, classes, interfaces, and imports.
 */

import { EventEmitter } from 'events';
import type { Config } from './config';

/** Greeting helper that returns a formatted string. */
export function greet(name: string): string {
    return `Hello, ${name}!`;
}

function _privateHelper(): void {
    // not exported
}

/**
 * Represents a user in the system.
 * Tracks name and email.
 */
export class UserService {
    private name: string;

    constructor(name: string) {
        this.name = name;
    }

    getName(): string {
        return this.name;
    }

    updateEmail(email: string): void {
        // update logic
    }
}

/** Configuration options for the app. */
export interface AppConfig {
    port: number;
    host: string;
    debug: boolean;
}

function calculateScore(items: number[]): number {
    return items.reduce((a, b) => a + b, 0);
}
