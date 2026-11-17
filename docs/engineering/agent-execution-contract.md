# Agent execution contract

This is the standard for an AI coding agent changing Apex. `AGENTS.md` and `docs/architecture/apex-sovereignty.md` still apply.

For Apex AI milestone assignments, the expected workflow is implementation → testing → documentation → commit → push → GitHub Actions verification → PR update → final report.

The user must not need to issue another prompt simply to get an authorized implementation committed and pushed.

## Required sequence

1. Read `docs/architecture/apex-sovereignty.md`.
2. Read the relevant decision records and `docs/status.md`.
3. Inspect the current code before assuming how a subsystem works.
4. Reuse an existing module before adding a package, service, or provider-specific type to a core contract.
5. Plan the work internally, then carry out the whole authorized assignment.
6. Finish every phase in that assignment. Do not stop for a confirmation of the next phase.
7. Preserve existing work. Do not force-push, rebase published commits, amend published commits, delete branches, or change the default branch unless the assignment explicitly says so.
8. Implement the behavior. Do not leave a placeholder where the assignment requires a working path.
9. Test the affected system, including the existing regression suite. Do not delete or weaken a failing test to make the suite green.
10. Update `docs/status.md` and the docs that describe the behavior that changed.
11. Commit and push when the assignment or the standing project execution policy authorizes it.
12. Wait for GitHub Actions on the commit that was pushed and read the result.
13. If a required check fails, fix the cause, test it, commit the correction, and push again.
14. Open or update the pull request named by the assignment.
15. Do not merge unless the assignment explicitly authorizes the merge.
16. Report the commands, counts, commit SHA, Actions run, and pull request that were actually observed.
17. Keep a known limitation in the docs. Do not delete it because the milestone moved on.
18. Do not claim a test, deployment, integration, or review succeeded without the output that shows it.

## Boundaries that do not move

Identity comes from the validated bearer token. A model response cannot grant permission, approve an operation, register a capability, or mark a deterministic check passed. A connector, device, or remote agent is not a principal. Execution stays in `apex.execution`. Media, transcripts, and external responses are untrusted or derived input. Provider SDK objects stay inside adapters.

A milestone that authorizes commits and pushes still does not authorize a merge, a force-push, a history rewrite, a published GitHub release, or a production deployment. The v1.0 release decision stays with the user.
