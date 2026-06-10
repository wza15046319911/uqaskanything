# Code Style — Frontend (Vite + React + TypeScript)

Stack: Vite + React 18 + TypeScript. In dev, `/api` is proxied to the FastAPI
backend (`127.0.0.1:8077`) — see `vite.config.ts`.

## Formatting

ESLint (`eslint.config.js`) and Prettier (`.prettierrc`) are the source of truth
for linting and formatting. Run `npm run lint` and `npm run format` before
committing — do not fight or override their output; change the config instead.
Style baseline (enforced by Prettier): 2-space indent, single quotes, no trailing
semicolons, `printWidth` 100.

## Functions

- Use **function declarations** for all exported/public functions — components,
  hooks, API helpers, utilities.
- Arrow functions are only for **private/internal use**: event handlers,
  callbacks, and inline functions inside a component or module.
- **Inside `useEffect`**: named functions must use function declarations.
- **Naming**: non-component functions use **camelCase** (`fetchAsk`,
  `handleSubmit`, `formatUnits`). Components use **PascalCase**.

  ```tsx
  // Do — exported functions use declarations
  export function fetchAsk(question: string) { ... }
  export default function AskPanel() { ... }

  // Do — private functions inside a component use arrow functions
  const handleSubmit = () => { ... };

  // Don't — exported arrow functions
  export const fetchAsk = (question: string) => { ... };

  // Do — declaration inside useEffect
  useEffect(() => {
    async function load() { ... }
    load();
  }, []);
  ```

## Components

- Always function declarations, never arrow-function variables.
- **Single-component file** (one component, or main + private helpers): the main
  component uses `export default`; helpers are not exported.
- **Multiple shared components in one file**: named exports for all, no default.
- Each component **owns its props type** (`interface XxxProps`) in the same file.
  Do not import types from pages/features into shared components — dependency
  direction is page → component, never the reverse.

  ```tsx
  function CourseRow({ code, title }: CourseRowProps) {
    return (
      <li>
        <b>{code}</b> {title}
      </li>
    )
  }

  export default function CourseList({ courses }: CourseListProps) {
    return (
      <ul>
        {courses.map((c) => (
          <CourseRow key={c.code} {...c} />
        ))}
      </ul>
    )
  }
  ```

## Data & API

- Keep `fetch` out of components. Put API helpers in `src/api/`
  (e.g. `src/api/ask.ts`, `src/api/sim.ts`) and call them from components.
- Define response types next to the helper (or in `src/types.ts`), with fields
  matching the backend JSON (`/api/ask`, `/api/sim/*`).

## File & Folder Naming

- **Component files**: PascalCase (`AskPanel.tsx`, `CourseList.tsx`).
- **Non-component `.ts`** (api, hooks, utils, types): kebab-case (`ask.ts`,
  `use-ask.ts`, `format-units.ts`).
- **Folders**: PascalCase for component folders, kebab-case for everything else.
- Keep Vite entry files as-is: `main.tsx`, `App.tsx`, `index.html`,
  `vite.config.ts`.
- If `shadcn/ui` is added later, its components live in `src/components/ui/` and
  use kebab-case (library convention).

> This is a Vite app, **not** Next.js — there is no `src/app/` router, no server
> actions, and no RSC. Do not assume those exist.
