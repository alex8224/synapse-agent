namespace GameControl.Cli;

/// <summary>Entry point for the bounded local game-control command line.</summary>
public static class Program
{
    public static int Main(string[] args) => WindowsCapture.App.GameControlCli.Run(args);
}
