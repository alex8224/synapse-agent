use std::fs;
use std::io;
use std::path::Path;

use chardetng::EncodingDetector;
use encoding_rs::{Encoding, UTF_16BE, UTF_16LE, UTF_8};

#[derive(Debug, Clone, Copy)]
pub(crate) struct FileEncoding {
    encoding: &'static Encoding,
    has_bom: bool,
}

impl FileEncoding {
    fn utf8(has_bom: bool) -> Self {
        Self {
            encoding: UTF_8,
            has_bom,
        }
    }

    fn encode(self, text: &str) -> Vec<u8> {
        match self.encoding.name() {
            "UTF-16LE" => encode_utf16(text, self.has_bom, true),
            "UTF-16BE" => encode_utf16(text, self.has_bom, false),
            _ => {
                let mut output = Vec::with_capacity(text.len() + 3);
                if self.has_bom && self.encoding == UTF_8 {
                    output.extend_from_slice(&[0xEF, 0xBB, 0xBF]);
                }
                let (encoded, _, _) = self.encoding.encode(text);
                output.extend_from_slice(&encoded);
                output
            }
        }
    }
}

fn encode_utf16(text: &str, has_bom: bool, little_endian: bool) -> Vec<u8> {
    let mut output = Vec::with_capacity(text.len() * 2 + usize::from(has_bom) * 2);
    if has_bom {
        output.extend_from_slice(if little_endian {
            &[0xFF, 0xFE]
        } else {
            &[0xFE, 0xFF]
        });
    }
    for unit in text.encode_utf16() {
        let bytes = if little_endian {
            unit.to_le_bytes()
        } else {
            unit.to_be_bytes()
        };
        output.extend_from_slice(&bytes);
    }
    output
}

pub(crate) fn read_text_detect(path: &Path) -> io::Result<(String, FileEncoding)> {
    let bytes = fs::read(path)?;
    detect_and_decode(&bytes)
}

pub(crate) fn write_text_as(path: &Path, content: &str, encoding: FileEncoding) -> io::Result<()> {
    fs::write(path, encoding.encode(content))
}

fn detect_and_decode(bytes: &[u8]) -> io::Result<(String, FileEncoding)> {
    if let Some(rest) = bytes.strip_prefix(&[0xEF, 0xBB, 0xBF]) {
        let text = std::str::from_utf8(rest)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        return Ok((text.to_owned(), FileEncoding::utf8(true)));
    }
    if bytes.starts_with(&[0xFF, 0xFE]) {
        return decode_utf16(bytes, UTF_16LE);
    }
    if bytes.starts_with(&[0xFE, 0xFF]) {
        return decode_utf16(bytes, UTF_16BE);
    }
    if let Ok(text) = std::str::from_utf8(bytes) {
        return Ok((text.to_owned(), FileEncoding::utf8(false)));
    }

    let mut detector = EncodingDetector::new();
    detector.feed(bytes, true);
    let encoding = detector.guess(None, true);
    let (text, had_errors) = encoding.decode_without_bom_handling(bytes);
    if had_errors {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "failed to decode file content (detected encoding: {})",
                encoding.name()
            ),
        ));
    }
    Ok((
        text.into_owned(),
        FileEncoding {
            encoding,
            has_bom: false,
        },
    ))
}

fn decode_utf16(bytes: &[u8], encoding: &'static Encoding) -> io::Result<(String, FileEncoding)> {
    let (text, _, had_errors) = encoding.decode(bytes);
    if had_errors {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("invalid {} content", encoding.name()),
        ));
    }
    Ok((
        text.into_owned(),
        FileEncoding {
            encoding,
            has_bom: true,
        },
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use encoding_rs::GBK;

    #[test]
    fn round_trips_utf8_bom() {
        let bytes = b"\xEF\xBB\xBFhello";
        let (text, encoding) = detect_and_decode(bytes).expect("decode UTF-8 BOM");
        assert_eq!(text, "hello");
        assert_eq!(encoding.encode(&text), bytes);
    }

    #[test]
    fn round_trips_detected_gbk() {
        let original = "你好，世界。edit me";
        let (bytes, _, had_errors) = GBK.encode(original);
        assert!(!had_errors);
        let (text, encoding) = detect_and_decode(&bytes).expect("decode GBK");
        assert_eq!(text, original);
        let edited = text.replace("edit me", "已修改");
        let (round_tripped, _) =
            detect_and_decode(&encoding.encode(&edited)).expect("decode edited GBK");
        assert_eq!(round_tripped, edited);
    }
}
