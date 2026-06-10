# Lessons Learned

Anti-patterns and practices learned from past mistakes. Follow these to avoid repeating errors.

## Only Implement What Is Requested

**Problem**: Over-engineering by adding extra features, props, abstractions, or "improvements" that were not asked for.

**Solution**: Stick strictly to the requirements given. Do not anticipate future needs or add "nice-to-have" features.

**Examples of over-engineering to avoid**:
- Adding customizable props when hardcoded values are sufficient
- Creating configuration options that weren't requested
- Adding error handling for scenarios that won't happen
- Building abstractions for one-time use cases
- Adding callbacks like `onClose`, `onClick` when not needed
- Making components "flexible" when they serve a single purpose

**Key points**:
- Ask yourself: "Did the user ask for this?" - if no, don't add it
- Simple, focused code is easier to maintain than flexible, complex code
- Extra features create extra maintenance burden
- If flexibility is needed later, it can be added then

## Always Reuse Existing Components Before Writing Custom Styles

**Problem**: Writing custom inline CSS/Tailwind classes for UI elements that already exist as components.

**Solution**: Before styling any UI element, check `src/components/` and `src/components/ui/` for existing components that can be composed together.

**Example** - Instead of custom button styling:

```tsx
// DON'T do this
<Link
  href="/subscribe"
  className="shrink-0 rounded-full border border-white/80 bg-transparent px-5 py-1.5 font-sans text-xs uppercase tracking-wider text-white transition-colors hover:bg-white/10"
>
  START TRIAL
</Link>

// DO this - compose existing components
<Button variant="badge" size="badge" asChild>
  <Link href="/subscribe">
    <Badge variant="default">START TRIAL</Badge>
  </Link>
</Button>
```

**Key points**:
- Check `Button`, `Badge`, and other UI components for available variants
- Use `asChild` prop (Radix Slot pattern) when wrapping `Link` components
- Composing components ensures consistency and maintainability
- Changes to the design system automatically propagate everywhere

## Ask for Clarification Instead of Assuming

**Problem**: Making assumptions about ambiguous terms or requests instead of asking the user for clarification.

**Solution**: When a term or request is ambiguous, ask a clarifying question before proceeding. This is especially important when:
- The conversation has a clear context that the interpretation might break
- The term could reasonably mean multiple things
- Your interpretation would be a shift in topic

**Example** - When asked about "workspace configuration" during a conversation about Claude instructions:

```
// DON'T do this
Assume "workspace configuration" means project config files (package.json, tsconfig, etc.)
and proceed to list them without confirming.

// DO this
Ask: "By workspace configuration, do you mean project config files (package.json, tsconfig, etc.)
or Claude-specific local settings (CLAUDE.local.md)?"
```

**Key points**:
- Stay in the context of the conversation flow
- Don't jump to conclusions based on common interpretations
- A quick clarifying question prevents wasted effort and frustration
- When uncertain, ask - don't guess

## Gather Context Yourself — Don't Ask the User About Code

**Problem**: Asking the user to confirm things about the codebase that can be answered by reading the code directly. Examples: "Want me to check whether this is cached?", "Is this function used elsewhere?", "Does this depend on X?". This offloads work to the user that should be done by reading files, grepping, and following the call graph.

**Solution**: When curious about how a function works, whether it's cached, who calls it, what it returns, or any other question with a definitive answer in the code — open the file and read it. Use Grep/Glob to find usages. Follow imports until you have the answer. Only ask the user when the question is about **intent, priorities, or genuinely ambiguous requirements** — not facts that exist in the codebase.

**Example**:

```
// DON'T do this
"One question before I touch it: does getChargebeePlans() need any caching?
 Want me to check whether that's already in place?"

// DO this
1. Read src/server-actions/chargebee/get-plans.ts
2. Glob src/server-actions/chargebee/* to check for unstable-cache.ts
3. Follow the import to src/lib/chargebee/plan-pricing.ts
4. Report findings: "Checked — getChargebeePlans is NOT cached. No
   unstable-cache.ts exists for chargebee. The underlying call hits
   Chargebee directly via chargebee.itemPrice.retrieve(). Here's the
   implication for your decision..."
```

**Key points**:
- Curiosity about code is your job to satisfy, not the user's burden
- Reserve user questions for what only the user knows: intent, priorities, ambiguous requirements, product decisions
- Read → Grep → Glob → follow imports → keep digging until you hit either an answer or a real ambiguity that needs human input
- Surface what you found and what it means, don't just dump tool output
- This contrasts with the "Ask for Clarification" rule below: that's about ambiguous user *intent*; this is about facts in the *code*

## Reusable Components Must Own Their Own Types

**Problem**: Making reusable components (layouts, UI primitives) import types from specific feature components (templates, pages), creating tight coupling and reverse dependencies.

**Solution**: Each reusable component should define its own types based on what IT needs. The consumer (template/page) transforms its data to match what the reusable component expects.

**Example** - WordsBy layout needing author data:

```typescript
// DON'T do this - layout depends on template
// src/layouts/WordsBy/types.d.ts
import { Author } from "@/templates/Articles/types";  // ❌ Wrong direction!

export interface WordsByProps {
  authors: Author[];
}

// DO this - layout owns its types
// src/layouts/WordsBy/types.d.ts
export type WordsByAuthor = {  // ✅ Own type definition
  name: string;
  image: string;
  slug: string;
  showLink: boolean;
};

export interface WordsByProps {
  authors: WordsByAuthor[];
}
```

**Key points**:
- Layouts and UI components should NEVER import from templates or pages
- Dependency direction: Templates → Layouts → UI (not the reverse)
- If Article's `Author` type adds a field, WordsBy shouldn't need to change
- Each component defines only the fields it actually uses
- This enables true reusability across different contexts

## Prefer Theme Tokens Over Hardcoded Values

**Problem**: Using hardcoded hex values and arbitrary Tailwind values (e.g., `text-[#333]`, `bg-[#333]`, `rounded-[22.5px]`) when equivalent theme tokens already exist in `src/app/globals.css`.

**Solution**: Before writing any color, size, or spacing value, check `globals.css` for existing CSS custom properties. Use the corresponding Tailwind token class instead of arbitrary values.

**Example** - Color and border-radius:

```css
/* globals.css already defines: */
--primary: #333333;
--text-md: 1.25rem;
```

```tsx
// DON'T do this - hardcoded hex values
<h1 className="text-[#333]">Title</h1>
<input className="border-[#333] rounded-[22.5px]" />
<button className="bg-[#333] text-[20px]">Submit</button>

// DO this - use theme tokens
<h1 className="text-primary">Title</h1>
<input className="border-primary rounded-full" />
<button className="bg-primary text-md">Submit</button>
```

**Key points**:
- Always check `globals.css` for existing CSS variables before using arbitrary values
- Common token mappings: `--primary` → `text-primary`/`border-primary`/`bg-primary`
- For pill shapes on fixed-height elements, `rounded-full` is cleaner than exact `rounded-[Xpx]`
- If a size token exists but has a different line-height, use the token and override just the line-height (e.g., `text-md leading-[140%]`)
- Hardcoded values break when the design system changes; tokens update automatically

## Verify Every Usage Before Removing or Renaming Anything

**Problem**: Removing or renaming a state, prop, function, type, export, or any shared code based on a partial search, then claiming it's safe — only to break consumers that were missed.

**Solution**: Before removing or renaming anything, search for **every usage** across the entire codebase and actually read each result. Do not skim grep output, do not assume based on a few matches, and do not declare "only used in X" without opening every file that references it.

**Example** - Removing `loading` state from an auth context:

```typescript
// DON'T do this
// Grep for "loading", see Profile.tsx, say "only used in one place", remove it.
// → Misses MyBroadsheet and SignupAccess which also destructure loading from useAuth()
// → App breaks, can't build.

// DO this
// 1. Search for every consumer of useAuth()
// 2. Open EACH file and check if it uses `loading`
// 3. Only after confirming every consumer does NOT use it, proceed with removal
```

**Key points**:
- This applies to everything: states, props, functions, types, exports, variables, CSS classes — not just shared interfaces
- "Grep and skim" is not verification. Read each result.
- If there are many consumers, check them all — not just the first few
- When in doubt, tell the user which files you checked and let them confirm before proceeding
