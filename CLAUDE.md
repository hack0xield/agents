# Working in this repo

Every process you start, you stop before finishing your turn — including
anything started only to check something.

Kill by port or explicit pid; a `pkill -f` pattern that appears in your own
command line kills your shell.

## Do not run the behavioural evals

`tests/run-evals.sh` costs real tokens per probe and is the founder's to run,
not yours. After changing a prompt, a tool description or a model, say which
probes are worth re-checking and leave it to them.

The other three suites are free and need no model — use those.
