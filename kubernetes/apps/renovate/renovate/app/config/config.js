module.exports = {
  // Global only options
  allowCustomCrateRegistries: true,
  allowPlugins: true,
  allowScripts: true,
  assigneesFromCodeOwners: true,
  autodiscover: true,
  autodiscoverFilter: ["shamubernetes/*", "kiliantyler/*"],
  baseDir: "/tmp/renovate",
  inheritConfig: true,
  inheritConfigFileName: "renovate.json5",
  inheritConfigRepoName: "renovate-config",
  onboarding: true,
  onboardingBranch: "shamubot/configure",
  onboardingConfigFileName: ".github/renovate.json5",
  onboardingNoDeps: "enabled",
  onboardingPrTitle: "Configure ShamuBot",
  onboardingRebaseCheckbox: true,
  persistRepoData: true,
  platform: "github",
  redisPrefix: "shamubot_",
  repositoryCache: "enabled",

  // Configurable options
  automergeStrategy: "merge-commit",
  automergeType: "pr",
  branchPrefix: "shamubot/",
  branchPrefixOld: "renovate/",
  dependencyDashboard: true,
  dependencyDashboardAutoclose: false,
  dependencyDashboardHeader: "",
  dependencyDashboardLabels: ["dash"],
  dependencyDashboardOSVVulnerabilitySummary: "unresolved",
  dependencyDashboardTitle: "Update Dashboard 🐳",
  enabled: "true",
  gitAuthor: "ShamuBot <148669015+shamubot[bot]@users.noreply.github.com>",
  pinDigests: true,
  prHourlyLimit: 0,
  prConcurrentLimit: 0,
  reviewersFromCodeOwners: true,
  semanticCommits: "enabled",
  timezone: "America/New_York",
  rebaseWhen: 'behind-base-branch'
}
