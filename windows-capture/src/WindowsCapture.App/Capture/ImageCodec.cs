using System.IO;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace WindowsCapture.App;

/// <summary>
/// BGRA &lt;-&gt; PNG conversion. WPF's imaging stack is used because it is part of the desktop
/// framework already referenced by the app, works off the UI thread as long as the results are
/// frozen, and needs no extra native dependency.
/// </summary>
public static class ImageCodec
{
    /// <summary>Encode a tightly packed BGRA buffer to PNG, optionally downscaling the longest edge.</summary>
    public static byte[] EncodePng(byte[] bgra, int width, int height, int maxEdge)
    {
        if (width <= 0 || height <= 0) throw new ArgumentException("invalid frame size");
        if (bgra.Length < width * height * 4) throw new ArgumentException("frame buffer is smaller than width*height*4");

        BitmapSource source = BitmapSource.Create(
            width, height, 96, 96, PixelFormats.Bgra32, null, bgra, width * 4);
        source.Freeze();

        var longest = Math.Max(width, height);
        if (maxEdge > 0 && longest > maxEdge)
        {
            var scale = (double)maxEdge / longest;
            var scaled = new TransformedBitmap(source, new ScaleTransform(scale, scale));
            scaled.Freeze();
            source = scaled;
        }

        var encoder = new PngBitmapEncoder();
        encoder.Frames.Add(BitmapFrame.Create(source));

        using var buffer = new MemoryStream();
        encoder.Save(buffer);
        return buffer.ToArray();
    }

    /// <summary>Decode a PNG back into a BGRA buffer. Used by <c>--self-test</c> to verify pixels.</summary>
    public static (byte[] Bgra, int Width, int Height) DecodePng(byte[] png)
    {
        using var stream = new MemoryStream(png, writable: false);
        var decoder = new PngBitmapDecoder(stream, BitmapCreateOptions.PreservePixelFormat, BitmapCacheOption.OnLoad);
        var frame = decoder.Frames[0];

        var converted = new FormatConvertedBitmap(frame, PixelFormats.Bgra32, null, 0);
        converted.Freeze();

        var width = converted.PixelWidth;
        var height = converted.PixelHeight;
        var stride = width * 4;
        var buffer = new byte[stride * height];
        converted.CopyPixels(buffer, stride, 0);
        return (buffer, width, height);
    }

    /// <summary>Reads one BGRA pixel.</summary>
    public static (byte B, byte G, byte R, byte A) PixelAt(byte[] bgra, int width, int x, int y)
    {
        var i = (y * width + x) * 4;
        return (bgra[i], bgra[i + 1], bgra[i + 2], bgra[i + 3]);
    }

    /// <summary>Fraction of pixels in a centred box that match <paramref name="expectedBgra"/>.</summary>
    public static double MatchRatioInCentre(byte[] bgra, int width, int height, uint expectedBgra, double boxFraction = 0.5)
    {
        var boxW = Math.Max(1, (int)(width * boxFraction));
        var boxH = Math.Max(1, (int)(height * boxFraction));
        var x0 = (width - boxW) / 2;
        var y0 = (height - boxH) / 2;

        long total = 0, matching = 0;
        for (var y = y0; y < y0 + boxH; y++)
        {
            for (var x = x0; x < x0 + boxW; x++)
            {
                var (b, g, r, a) = PixelAt(bgra, width, x, y);
                var value = (uint)(b | (g << 8) | (r << 16) | (a << 24));
                total++;
                if (value == expectedBgra) matching++;
            }
        }

        return total == 0 ? 0 : (double)matching / total;
    }
}
