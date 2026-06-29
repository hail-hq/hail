package cmd

import (
	"github.com/spf13/cobra"
)

func newCompletionCmd(opts *Options) *cobra.Command {
	cmd := &cobra.Command{
		Use:                   "completion <bash|zsh|fish>",
		Short:                 "Generate a shell completion script",
		DisableFlagsInUseLine: true,
		Long: `hail completion — emit a shell completion script.

Install (pick one):

  # bash (system-wide)
  hail completion bash | sudo tee /etc/bash_completion.d/hail

  # zsh (per-user)
  echo 'source <(hail completion zsh)' >> ~/.zshrc

  # fish (per-user)
  hail completion fish > ~/.config/fish/completions/hail.fish`,
		Args:      argsOrHelp(1, "<bash|zsh|fish>"),
		ValidArgs: []string{"bash", "zsh", "fish"},
		RunE: func(cmd *cobra.Command, args []string) error {
			switch args[0] {
			case "bash":
				return cmd.Root().GenBashCompletionV2(opts.Stdout, true)
			case "zsh":
				return cmd.Root().GenZshCompletion(opts.Stdout)
			case "fish":
				return cmd.Root().GenFishCompletion(opts.Stdout, true)
			default:
				return helpAndFail(cmd, "unsupported shell "+args[0]+"; supported: bash, zsh, fish")
			}
		},
	}
	return cmd
}
