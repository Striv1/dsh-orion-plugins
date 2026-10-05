import Foundation
import PDFKit

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("用法：pdf_text_extract.swift <PDF 路径>\n".utf8))
    exit(2)
}

let source = URL(fileURLWithPath: CommandLine.arguments[1])
guard let document = PDFDocument(url: source) else {
    FileHandle.standardError.write(Data("无法打开 PDF 文件\n".utf8))
    exit(3)
}

var pages: [[String: Any]] = []
for index in 0..<document.pageCount {
    let text = document.page(at: index)?.string ?? ""
    pages.append([
        "page": index + 1,
        "text": text,
        "character_count": text.filter { !$0.isWhitespace }.count,
    ])
}

let payload: [String: Any] = [
    "page_count": document.pageCount,
    "pages": pages,
]
let encoded = try JSONSerialization.data(withJSONObject: payload, options: [])
FileHandle.standardOutput.write(encoded)
