# backend_resolution fixture

Per-stack `envs/` with generic names. `stacks/api` has a backend file per environment.
`stacks/worker` has dev only: its `nonprod.tfvars` has no backend file, so worker:nonprod is
dropped. `stacks/batch` resolves its nonprod from a file outside its tree and its dev from a
`-backend-config` setting in the deploy script. `stacks/report` has no backend file at all, so
its state stays unresolved and its environments stay (their tfvars are the only proof it has,
no sibling shows otherwise).
