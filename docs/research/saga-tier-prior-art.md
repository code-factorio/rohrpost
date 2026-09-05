# Prior art: the tier above epic and how it rolls up

Research note for **How do other trackers model and roll up the tier above epic?**
(`saga-6`), part of the saga wayfinder.

Rohrpost today has the types `task | bug | spike | epic`, one structural field (`parent`),
one level of nesting (epic -> leaf), and a derived epic status (done when every child is
done). The planned `saga` type sits above epic: its children are epics or leaves, never
sagas, and its status is derived through the epics. This note surveys how five hosted
trackers and two agent-first CLI trackers shape the tier above epic, against primary
sources only (official docs, official changelogs, source repositories).

This is **evidence, not a recommendation.** The design decision belongs to the saga
tickets and an ADR.

Where a fact could not be verified from a primary source the text says so.

## Summary

- Every surveyed tracker links child to parent with a **single field on the child**
  (Jira `Parent`, Linear `Issue.parent`, GitHub `parent`, Shortcut `epic_id` /
  `parent_story_id`, Beads computed `parent`, Backlog.md `parentTaskId`). The one
  exception is Shortcut's epic -> objective link, which is a **list of objective ids on
  the epic**, so an epic may sit under several objectives.
- Above the epic, hosted trackers tend to switch from a *type* to a *container*: Linear
  project/initiative, GitHub milestone/project, Shortcut objective. Jira alone keeps the
  tier as a work type in the same hierarchy, and only on Premium/Enterprise.
- Depth is bounded either by a **numeric cap** (GitHub sub-issues: 8 levels; Linear
  sub-initiatives: 5 levels) or by **construction** (Jira: a parent must be exactly one
  configured level above; Linear: an issue belongs to at most one project; GitHub
  milestones and Backlog.md milestones do not nest). No tracker documents a cap for its
  epic-like tier being unbounded on purpose.
- **Status is stored, progress is derived** almost everywhere. Jira, Linear, GitHub and
  Shortcut all keep the parent's status as a user-set field and compute a separate
  progress number (percent, counts, or points) from the children. Automatic
  status change is an *opt-in automation* (Linear "Parent auto-close", Jira Automation
  rules) or an *explicit command* (Beads `bd epic close-eligible`), never the
  default. Backlog.md is the only surveyed tool whose container completion is purely
  derived, and it is a milestone with no stored status at all.
- Jira's Plans roll up dates, estimates, releases, sprints and teams, and progress by
  work item count looks only at the children *directly beneath*, while progress by
  estimate walks all levels. Neither writes back to the parent issue.

## Jira Cloud

**Tier name.** Jira ships three hierarchy levels: "a level for larger pieces of work
(level 1, by default called Epic), a level for standard work items (level 0, called
Story), and a level for smaller pieces of work (level -1, called Subtask)"
([Configure the work type hierarchy](https://support.atlassian.com/jira-cloud-administration/docs/configure-the-issue-type-hierarchy/)).
Levels above epic exist only on the paid tiers: "Jira Premium and Enterprise customers
can also create and manage additional levels in their work type hierarchy" (same page).
Atlassian does not fix a name: "you can also add levels above 1 and use these extra
levels to track your organization's larger initiatives ... Some common work types are
Legend, Odyssey, or Anthology"
([Configure custom hierarchy levels in your plan](https://support.atlassian.com/jira-software-cloud/docs/configure-custom-hierarchy-levels-in-advanced-roadmaps/)).
The "top-level planning" project template creates a work type called "Top-level
initiative" which "is mapped to the initiative type above the epic issue type"
([KB: Top-level initiative issue type created automatically](https://support.atlassian.com/jira/kb/top-level-initiative-issue-type-created-automatically-in-the-jira-cloud-site/)).

**What nests under it.** A level is a slot that admins bind to one or more work types:
"Give your level a name, and use the dropdown in the Jira work types column to
associate it with an work type(s)" ([Configure the work type
hierarchy](https://support.atlassian.com/jira-cloud-administration/docs/configure-the-issue-type-hierarchy/)).
Children must come from the level directly below: "You can only link a work item to a
parent of the corresponding hierarchy level as configured in your plan. For instance,
if you made a level called initiative which lives above epic, you can't link a story to
the initiative. You must first add the story to an epic, then that epic to an
initiative" ([Link work item to a parent in your
plan](https://support.atlassian.com/jira-software-cloud/docs/link-issue-to-a-parent-in-advanced-roadmaps/)).
Setting a parent of a custom top-level type requires Premium: "You need to have Premium
subscription to set parent to a work item of this type"
([KB: Need Premium subscription to set parent](https://support.atlassian.com/jira/kb/need-premium-subscription-to-set-parent-error-in-jira-cloud/)).

**Depth bound.** Every new level goes on top: "Select + Create level at the bottom of
the list of levels. A new level will be created at the top of the work type hierarchy"
([Configure the work type
hierarchy](https://support.atlassian.com/jira-cloud-administration/docs/configure-the-issue-type-hierarchy/)).
Depth is therefore bounded by the configured level list, and each edge is exactly one
level. *No primary source found that states a maximum number of custom levels.*
Re-shaping the list is destructive: "Changing your work type hierarchy will break
existing parent and child relationships between your work items" (same page).

**Link direction.** A single field on the child. Atlassian replaced the two older
fields with one: "The Epic link and Parent link fields on the issue view will soon be
replaced by a single Parent field" and "the Epic link and Parent link fields will be
replaced by a single Parent field" on create
([Introducing the new Parent field in company-managed
projects](https://support.atlassian.com/jira-software-cloud/docs/upcoming-changes-epic-link-replaced-with-parent/)).
Queries use `parent` in JQL: "You can use the parent field in JQL to search for an
epic's work items"
([Upcoming changes: Epic link data above the epic
level](https://support.atlassian.com/jira-software-cloud/docs/maintain-epic-link-data-for-issues-above-the-epic-level/)).

**Status and progress.** The parent's status is a stored workflow status; nothing in
the hierarchy pages derives it. Atlassian's own answer to "close the parent when all
children close" is an Automation rule: "The purpose of this article is to provide a way
to configure an automation rule that will automatically close the parent issue, once
the last sub-task was closed" and "The parent issue's workflow must permit the
transition to the destination status attempted by the rule"
([Automation KB: close the parent issue when all its sub-tasks are
closed](https://support.atlassian.com/automation/kb/close-the-parent-issue-when-all-its-sub-tasks-are-closed/)).

Plans (Premium) derive other values, not status: "you can tell your plan to infer values
of parent work items based on those of the child work items. This is referred to as a
roll-up" and the two options are "Dates - includes start and end dates" and "Others -
includes estimation values, releases, sprints, and teams"
([What are roll-ups in my
plan?](https://support.atlassian.com/jira-software-cloud/docs/roll-up-values-in-advanced-roadmaps/)).
Roll-ups are not persisted: "When you commit changes back to your Jira work items, these
rolled-up values aren't saved and the date field of your work item will be empty"
(same page).

Progress is derived two ways with different reach. By count: "The Progress (work item
count) function tracks how a work item is progressing based the statuses of the work
items directly beneath it. This method of tracking progress doesn't take into account
any lower hierarchy levels. For example, if you have a level 2 hierarchy level called
Initiative that sits above a level 1 called Epic, the progress bar on the Initiative
reflects the progress of the Epics beneath it but not any work items nested within
those Epics"
([Track progress using work item count in your
plan](https://support.atlassian.com/jira-software-cloud/docs/track-progress-using-issue-count-in-advanced-roadmaps/)).
By estimate: "this method of tracking progress includes estimates from child work items
in all hierarchy levels, not just those directly beneath the selected one"
([Track progress using estimates in your
plan](https://support.atlassian.com/jira-software-cloud/docs/track-progress-using-estimates-in-advanced-roadmaps/)).

## Linear

Linear has three nested containers above the issue: sub-issue -> issue, issue ->
project, project -> initiative (-> parent initiative). "Initiatives sit above projects
and represent broader strategic efforts" and "Initiatives can also have parent and
sub-initiative relationships, which allows complex work to be broken into smaller
strategic areas while still rolling up into a larger objective"
([Conceptual model](https://linear.app/docs/conceptual-model)).
"Roadmaps are renamed to Initiatives" ([Projects](https://linear.app/docs/projects)).

### Issue -> parent issue

**Link.** A single `parent: Issue` field on the child, with a `children` connection on
the parent ([SDK GraphQL schema, `type
Issue`](https://github.com/linear/linear/blob/master/packages/sdk/src/schema.graphql)).
"When you add a sub-issue to another issue, the other issue becomes its 'parent'"
([Parent and sub-issues](https://linear.app/docs/parent-and-sub-issues)).

**Status.** Stored, with opt-in automation per team: "Parent auto-close: When all
sub-issues are marked as done, the parent issue will also be marked as done
automatically" and "Sub-issue auto-close: When the parent issue is marked as done, all
remaining sub-issues will also be marked as done", both under "Optionally, configure the
following behaviors at the team level" (same page).

**Depth.** *No primary source found that states a nesting limit for sub-issues.*

### Issue -> project

**Link.** Single field: "The project that the issue is associated with. Null if the
issue is not part of any project" (`project: Project` on `type Issue`, [SDK
schema](https://github.com/linear/linear/blob/master/packages/sdk/src/schema.graphql)).
"Issues can only be associated with one project at a time. A workaround would be to
create sub-issues for the task, then assign each sub-issue to a different project"
([Projects](https://linear.app/docs/projects)).

**Status.** Stored and explicitly not derived: "Project statuses are updated
manually—we do not do this automatically, even if all issues are completed"
([Project status](https://linear.app/docs/project-status)).
`status: ProjectStatus!` with categories `backlog, planned, started, paused, completed,
canceled` (`enum ProjectStatusType`, SDK schema).

**Progress.** Derived from estimates: "The overall progress of the project. This is the
(completed estimate points + 0.25 * in progress estimate points) / total estimate
points" (`progress: Float!` on `type Project`, SDK schema). Project milestones also carry
"The progress % of the project milestone" (`ProjectMilestone.progress`, SDK schema).
Health is not derived from issues but "derived from the most recent project update"
(`Project.health`, SDK schema).

### Project -> initiative

**Link.** A list on the child side and a join object: `Project.initiatives` is
"Initiatives that this project belongs to" (a connection), backed by `type
InitiativeToProject` with `initiative: Initiative!` and `project: Project!` ([SDK
schema](https://github.com/linear/linear/blob/master/packages/sdk/src/schema.graphql)).
The docs describe projects as the content: "Show the work streams contributing to the
initiative" ([Initiatives](https://linear.app/docs/initiatives)).

**Status.** Stored: "Communicate the current stage of the initiative using available
statuses—Proposed, Planned, Active, Completed, or Canceled. Keep status up to date"
(same page); `status: InitiativeStatus!` in the schema. Health is again from updates:
"Initiative Health shows whether the latest initiative update indicated work was on
track, at risk, or off track" (same page).

**Progress.** Derived for display: "Active Projects rolls up data for individual
projects in the initiative (including projects from any sub-initiatives) based on each
project's latest project update" and "Each curve on an initiative graph represents the
rate of completed issues within a single project in that initiative" (same page).

### Initiative -> parent initiative

**Depth.** Numeric cap, Enterprise only: "Sub-initiatives let you nest Initiatives up to
five levels deep in a tree-like structure. A parent Initiative automatically includes
all the projects it owns directly, as well as all the projects from its
sub-initiatives" and "Available to workspaces on our Enterprise plan"
([Sub-initiatives](https://linear.app/docs/sub-initiatives)).

**Link.** Not single-parent: "Initiatives can have multiple parents. Projects added to
any sub-initiative will automatically be included in the parent's view of progress"
(same page). The schema exposes both `parentInitiative: Initiative` and a
`subInitiatives` connection on `type Initiative` (SDK schema).

## GitHub

GitHub offers three different things above a leaf issue: sub-issue hierarchies (issues
under issues), milestones (flat grouping per repository), and Projects (cross-repository
tables). None is named "epic"; "issue types" are a flat label-like classification.

### Sub-issues

**Depth and fan-out.** "You can add up to 100 sub-issues per parent issue and create up
to eight levels of nested sub-issues"
([Adding sub-issues](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/adding-sub-issues);
the limit is the docs variable `sub-issue_limit: '100'` in
[`data/variables/projects.yml`](https://github.com/github/docs/blob/main/data/variables/projects.yml)).
The feature became generally available on 2025-04-09
([Changelog: Evolving GitHub Issues and Projects](https://github.blog/changelog/2025-04-09-evolving-github-issues-and-projects/)).

**Link.** Single parent on the child. GraphQL: "The parent entity of the issue"
(`parent: Issue` on `type Issue`) and "A list of sub-issues associated with the Issue"
(`subIssues` connection) ([octokit/graphql-schema
`schema.graphql`](https://github.com/octokit/graphql-schema/blob/master/schema.graphql)).
REST exposes `GET /repos/{owner}/{repo}/issues/{issue_number}/parent`, and "Add
sub-issue" takes `replace_parent`: "Option that, when true, instructs the operation to
replace the sub-issues current parent issue"
([REST: sub-issues](https://docs.github.com/en/rest/issues/sub-issues)).
CLI: `gh issue create --parent`, `gh issue edit --add-sub-issue`, `--remove-parent`
([Adding sub-issues](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/adding-sub-issues)).

**Status and progress.** State is a stored field (`state: IssueState!`). Progress is a
derived summary: "Summary of the state of an issue's sub-issues" (`subIssuesSummary:
SubIssuesSummary!`) with "Count of completed sub-issues", "Percent of sub-issues which
are completed", "Count of total number of sub-issues" ([octokit
schema](https://github.com/octokit/graphql-schema/blob/master/schema.graphql)).
Projects can show it: "You can enable the 'Sub-issue progress' field to see how many
sub-issues have been completed"
([About parent issue and sub-issue progress
fields](https://docs.github.com/en/issues/planning-and-tracking-with-projects/understanding-fields/about-parent-issue-and-sub-issue-progress-fields)).
*No primary source found that states the parent's open/closed state is derived from
its sub-issues.* Inheritance goes downward: "Sub-issues now inherit the Project and
Milestone of their parent issue by default"
([Changelog 2025-09-11](https://github.blog/changelog/2025-09-11-a-rest-api-for-github-projects-sub-issues-improvements-and-more/)).

### Milestones

**Children.** "you can associate it with issues and pull requests"
([About milestones](https://docs.github.com/en/issues/using-labels-and-milestones-to-track-work/about-milestones)).
The issue side is a single field: "Identifies the milestone associated with the issue"
(`milestone: Milestone` on `type Issue`, [octokit
schema](https://github.com/octokit/graphql-schema/blob/master/schema.graphql)).

**Depth.** Milestones are flat. *No primary source describes milestone nesting.*

**Status and progress.** State is caller-set: the create endpoint's `state` is "The
state of the milestone. Either open or closed", default `open`
([REST: milestones](https://docs.github.com/en/rest/issues/milestones)).
Progress is derived: "The milestone's completion percentage" and "The number of open
and closed issues and pull requests associated with the milestone"
([About milestones](https://docs.github.com/en/issues/using-labels-and-milestones-to-track-work/about-milestones));
GraphQL "Identifies the percentage complete for the milestone" (`progressPercentage:
Float!`), REST `open_issues` / `closed_issues` on the milestone object.

### Projects

Projects hold "issues and pull requests" plus "draft issues"
([About Projects](https://docs.github.com/en/issues/planning-and-tracking-with-projects/learning-about-projects/about-projects)).
Project-level status is a manual status update: "You can set a status, such as 'On
track' or 'At risk', to allow people to quickly determine the current state of the
project" (same page). Aggregation is via charts: "insights ... charts that use the items
added to your project as their source data" (same page). *No primary source describes
projects nesting in projects.*

## Shortcut

**Hierarchy.** Stories -> Epics -> Objectives. "An Objective is Shortcut's top-level
planning object and helps an Organization set goals, align Teams, and connect work to
company outcomes" and "Tactical Objectives, which are Epic-driven -> Use them to roll up
delivery work"
([Objectives Overview](https://www.shortcut.com/help/objectives/objectives-overview/)).
The API says the same: "An Objective is a collection of Epics that represent a release
or some other large initiative that you are working on"
([REST API v3, `Objective`](https://developer.shortcut.com/api/rest/v3)).
Objectives replaced Milestones: the epic field `milestone_id` is "Deprecated The ID of
the Milestone this Epic is related to. Use objective_ids" (API v3, Create Epic).

**Link direction.** Two different shapes:

- Story -> epic: single field `epic_id Integer or null` on the story (API v3, Story).
- Epic -> objective: a **list on the child**, `objective_ids Array [Integer]`, "An array
  of IDs for Objectives to which this Epic is related" (API v3, Create Epic). An epic can
  therefore belong to several objectives; the objective side reports "Epic Progress
  (total of all progress across all unique Epics assigned to the Objective)"
  ([Objectives Overview](https://www.shortcut.com/help/objectives/objectives-overview/)).
- Story -> parent story (sub-tasks): single field `parent_story_id Integer or null`,
  "The ID of the parent story to this story (making this story a sub-task)", mirrored by
  `sub_task_story_ids Array [Integer]` on the parent (API v3, Story).

**Depth.** Objectives hold epics; epics hold stories; stories hold sub-tasks. *No primary
source found that says sub-tasks can nest further, and none that says objectives nest.*
Sub-tasks inherit upward context: "Sub-tasks created in a Parent Story will inherit the
Epic and Team fields" ([Sub-tasks](https://www.shortcut.com/help/stories/sub-tasks/)).

**Status.** Stored at every level. Objective: the update endpoint takes `state Enum
(done, in progress, to do)` "The workflow state that the Objective is in" (API v3,
Update Objective), and the detail page lets you "Update core fields in the right sidebar
(Owners, Teams, dates, State, Category)"
([Objectives Overview](https://www.shortcut.com/help/objectives/objectives-overview/)).
Epic: "Epic States correspond to one of 3 types: Unstarted, Started, or Done" (API v3,
`EpicState`), changed by hand "From the Epic Page: select the state from the dropdown in
the sidebar" or "drag and drop Epics across columns"
([Epics Overview](https://www.shortcut.com/help/epics/epics-overview/)).
*An older help-centre snippet stated that a parent story moves to Done when all sub-tasks
are done; the current Sub-tasks page does not contain that sentence, so it is treated as
unverified here.*

**Progress.** Derived and exposed as read-only stats. `EpicStats` is "A group of
calculated values for this Epic" with `num_stories_done`, `num_stories_total`,
`num_points_done` and so on (API v3). Objective pages show "Epic Progress (Tactical +
Strategic) rolls up progress across all unique Epics on the Objective"
([Objectives Overview](https://www.shortcut.com/help/objectives/objectives-overview/)).

## Beads (agent-first CLI)

The repository `steveyegge/beads` now redirects to
[`gastownhall/beads`](https://github.com/gastownhall/beads); links below use the new
location.

**Tier name.** There is no tier above epic. Built-in types are `bug, feature, task,
epic, chore, decision, message, molecule, gate, spike, story, milestone`
([`internal/types/types.go`](https://github.com/gastownhall/beads/blob/main/internal/types/types.go)).
`milestone` is a type, not a container: "Marks completion of a set of related issues (no
work itself)" (same file). The README shows the intended shape as three named levels:
"Beads supports hierarchical IDs for epics: `bd-a3f8` (Epic) / `bd-a3f8.1` (Task) /
`bd-a3f8.1.1` (Sub-task)"
([README](https://github.com/gastownhall/beads/blob/main/README.md)).

**Link.** Parent-child is one of the dependency edge types, stored in the dependency
table (`IssueID`, `DependsOnID`, `Type`): `DepParentChild DependencyType =
"parent-child"` under "Workflow types (affect ready work calculation)"
([`types.go`](https://github.com/gastownhall/beads/blob/main/internal/types/types.go)).
The API projects it back as a single value: `Parent *string` "Computed parent from
parent-child dep" (same file). The CLI treats it as single-valued: "Move issues between
epics with `bd update <id> --parent=<new-parent>`" and "Supports clearing parent with
`--parent=none`" ([CHANGELOG](https://github.com/gastownhall/beads/blob/main/CHANGELOG.md));
`bd create --parent string` "Parent issue ID for hierarchical child"
([`bd create`](https://github.com/gastownhall/beads/blob/main/docs/cli-reference/create.md)).
Children are listed with `bd children <parent-id>`, "a convenience alias for 'bd list
--parent <id> --status all'"
([`bd children`](https://github.com/gastownhall/beads/blob/main/docs/cli-reference/children.md)).
A guard forbids the inverse edge: "Epic children can no longer depend on their parent
epic" ([CHANGELOG](https://github.com/gastownhall/beads/blob/main/CHANGELOG.md)).

**Depth.** *No primary source found that bounds nesting depth.* The only depth number
is a display limit: `bd dep tree --max-depth int` "Maximum tree depth to display (safety
limit) (default 50)"
([`bd dep`](https://github.com/gastownhall/beads/blob/main/docs/cli-reference/dep.md)).

**Status.** Stored; closure is derived-eligible but explicit. The detail view carries
"Epic progress fields (populated only for issue_type=epic with children)":
`epic_total_children`, `epic_closed_children`, `epic_closeable`
([`types.go`](https://github.com/gastownhall/beads/blob/main/internal/types/types.go)).
Closing is a command: `bd epic close-eligible` "Close epics where all children are
complete" with `--dry-run`, and `bd epic status` "Show epic completion status"
([`bd epic`](https://github.com/gastownhall/beads/blob/main/docs/cli-reference/epic.md)).
The reverse is guarded: "Epic close guards — prevents accidental closure of epics with
open children" ([CHANGELOG](https://github.com/gastownhall/beads/blob/main/CHANGELOG.md)).

## Backlog.md (agent-first Markdown CLI)

**Tier name.** Two structures: parent tasks (sub-tasks) and milestones. There is no
epic type; milestones are the grouping tier. "Milestones & dependencies -- structure
bigger efforts and make execution order reviewable"
([README](https://github.com/MrLesk/Backlog.md/blob/main/README.md)).

**Link.** Single field on the child in both cases. Sub-task: "Create sub task |
`backlog task create -p 14 "Add Login with Google"`" and list output carries
`parentTaskId`; milestone: task list rows carry a single `milestone` value
([CLI-INSTRUCTIONS.md](https://github.com/MrLesk/Backlog.md/blob/main/CLI-INSTRUCTIONS.md)).
Milestones are files managed by `backlog milestone add|rename|remove|archive`
(same page).

**Depth.** *No primary source found that states a sub-task nesting limit, and none that
lets milestones nest.*

**Status.** Milestone completion is purely derived at read time in
[`src/core/milestones.ts`](https://github.com/MrLesk/Backlog.md/blob/main/src/core/milestones.ts):

```ts
const doneCount = bucketTasks.filter((t) => isDoneStatus(t.status)).length;
const progress = bucketTasks.length > 0 ? Math.round((doneCount / bucketTasks.length) * 100) : 0;
const isCompleted = bucketTasks.length > 0 && doneCount === bucketTasks.length;
```

where `isDoneStatus` matches a status containing "done" or "complete". The same
"derived, never stored" rule is documented for readiness: "`isReady` is derived from the
whole visible corpus at read time and never stored"
([CLI-INSTRUCTIONS.md](https://github.com/MrLesk/Backlog.md/blob/main/CLI-INSTRUCTIONS.md)).
*No primary source found on whether a parent task's status is derived from its
sub-tasks.*

## Comparison

| Tracker | Tier above epic | Children allowed | Status derived or stored | Depth bound | Link direction |
|---|---|---|---|---|---|
| Jira Cloud | Custom level above Epic (e.g. "Initiative", "Top-level initiative"); Premium/Enterprise only | Work types bound to the level directly below | Status stored; Plans derive dates/estimates/progress for display only (count = direct children, estimate = all levels) | By configured level list; each parent exactly one level up; no documented cap | Single `Parent` field on child |
| Linear | Project above issue; Initiative above project; parent initiative above initiative | Project: issues (one project per issue); Initiative: projects and sub-initiatives | Status stored (project "updated manually"); progress derived from estimate points; health from updates | Issue->project: 1 level; sub-initiatives: 5 levels | `Issue.parent`, `Issue.project` single on child; project->initiative many-to-many join; initiatives "can have multiple parents" |
| GitHub | Sub-issue parent (no epic type); Milestone; Project | Sub-issues: issues; Milestone: issues and PRs; Project: issues, PRs, drafts | State stored; `subIssuesSummary.percentCompleted` and milestone `progressPercentage` derived | Sub-issues: 8 levels, 100 per parent; milestones and projects flat | `parent` single on child (`replace_parent` on re-link); `milestone` single on issue |
| Shortcut | Objective above Epic (Tactical = epic-driven) | Objective: epics (and key results); Epic: stories; Story: sub-tasks | State stored at objective, epic and story; `EpicStats` and "Epic Progress" derived | Objective->epic->story->sub-task; no documented nesting of objectives or sub-tasks | Story->epic `epic_id` single; epic->objective `objective_ids` **list on child**; sub-task `parent_story_id` single |
| Beads | None (epic is top; `milestone` is a leaf type) | Epic: any issue via `parent-child` edge | Status stored; `epic_closeable` derived; closure via explicit `bd epic close-eligible` | No documented bound (display limit 50) | `parent-child` dependency row, projected as single computed `parent` |
| Backlog.md | Milestone (no epic type); parent task | Milestone: tasks; parent task: sub-tasks | Milestone `isCompleted`/`progress` fully derived at read time; no stored milestone status | Milestones flat; sub-task depth undocumented | `milestone` and `parentTaskId` single on child |
