# Taro milk full UI and dark mode implementation plan

> **For implementation:** Apply this plan directly in the existing Fecho working tree. The tree already contains the approved preview and brand work, so preserve those uncommitted changes.

**Goal:** Move the approved taro-purple, cream, and butter-yellow direction from the preview into every shipped Fecho page, with a complete dark theme that uses the same visual hierarchy.

**Design decisions:**

- Light canvas is soft cream (`#FFF9EC`), never a lavender wash.
- Taro (`#A99BB5`) and pale taro (`#D8C6DC`) create structure; butter yellow (`#F0C75F`) is reserved for the primary action and visible product mark.
- The browser favicon stays purple. The in-page mark is a round yellow, button-like plus with a small press response and no navigation.
- Product typography stays system-native and sans-serif: Avenir Next / SF Pro Rounded / SF Pro Text / PingFang SC / Microsoft YaHei.
- Dark mode gets deep plum surfaces, warm white text, lavender hierarchy, and the same yellow primary action. It shares the existing `fecho-theme` storage key and theme controls.

**Implementation outline:**

1. Write static contract tests for the palette, dark-mode entry points, in-page mark, and purple favicon.
2. Replace Dashboard tokens and sidebar brand; make the primary button, selection, cards, hero, and editor tokens consume the system.
3. Apply the shared token family and typography to sign-in, setup, and guide pages. Use yellow in-page marks where a brand mark is shown.
4. Run focused template tests, then the full suite and static checks.
