import Foundation
import Metal
import simd

let width = 1365
let height = 768
let draws = 826
let writes = 363
let recordCount = 8
let stride = 2048
let recordSize = writes * stride
let frames = 200
let markerStride = 256
let payloadSizes = [1056, 1296, 1584]
let device = MTLCreateSystemDefaultDevice()!
let queue = device.makeCommandQueue()!
let library = try device.makeLibrary(source: """
#include <metal_stdlib>
using namespace metal;
vertex float4 vertex_position(const device float2* points [[buffer(0)]],
                              const device float2* grid [[buffer(1)]], uint id [[vertex_id]]) {
    const float2 p = grid[id];
    return float4(points[0] + p.x * (points[1] - points[0]) + p.y * (points[2] - points[0]), 0.0, 1.0);
}
fragment float4 fragment_color(constant float4& color [[buffer(0)]]) {
    return color;
}
""", options: nil)
let pipelineInfo = MTLRenderPipelineDescriptor()
pipelineInfo.vertexFunction = library.makeFunction(name: "vertex_position")
pipelineInfo.fragmentFunction = library.makeFunction(name: "fragment_color")
pipelineInfo.colorAttachments[0].pixelFormat = .rgba8Unorm
let pipeline = try device.makeRenderPipelineState(descriptor: pipelineInfo)
let textureInfo = MTLTextureDescriptor.texture2DDescriptor(
    pixelFormat: .rgba8Unorm, width: width, height: height, mipmapped: false
)
textureInfo.usage = .renderTarget
let texture = device.makeTexture(descriptor: textureInfo)!
let stream = device.makeBuffer(length: recordCount * recordSize, options: .storageModeShared)!
let readback = device.makeBuffer(length: frames * markerStride, options: .storageModeShared)!

// Tessellation frequencies approximate the primitive counts in one raid frame.
let frequencies: [(subdivisions: Int, draws: Int)] = [
    (1, 103), (2, 153), (3, 63), (4, 42), (5, 27), (6, 93),
    (7, 30), (8, 26), (9, 19), (10, 40), (11, 13), (12, 11),
    (13, 13), (14, 8), (15, 13), (16, 26), (17, 10), (18, 41),
    (19, 4), (20, 3), (21, 7), (22, 5), (23, 23), (24, 1),
    (25, 6), (26, 2), (27, 1), (28, 29), (29, 4), (30, 1),
    (31, 1), (32, 8),
]
let levels = frequencies.flatMap { Array(repeating: $0.subdivisions, count: $0.draws) }
precondition(levels.count == draws)
var ranges: [Int: Range<Int>] = [:]
var unitVertices: [SIMD2<Float>] = []
for level in Set(levels).sorted() {
    let start = unitVertices.count
    let divisor = Float(level)
    for i in 0..<level {
        for j in 0..<(level - i) {
            let a = SIMD2(Float(i), Float(j)) / divisor
            let b = SIMD2(Float(i + 1), Float(j)) / divisor
            let c = SIMD2(Float(i), Float(j + 1)) / divisor
            unitVertices.append(contentsOf: [a, b, c])
            if i + j + 1 < level {
                let d = SIMD2(Float(i + 1), Float(j + 1)) / divisor
                unitVertices.append(contentsOf: [b, d, c])
            }
        }
    }
    precondition(unitVertices.count - start == 3 * level * level)
    ranges[level] = start..<unitVertices.count
}
let drawRanges = (0..<draws).map { ranges[levels[($0 * 593) % draws]]! }
let grid = device.makeBuffer(bytes: unitVertices,
                            length: unitVertices.count * MemoryLayout<SIMD2<Float>>.stride,
                            options: .storageModeShared)!
let triangleTotal = drawRanges.reduce(0) { $0 + $1.count / 3 }
print("\(draws) draws and \(triangleTotal) synthetic triangles per output")

func blue(_ frame: Int) -> UInt8 { UInt8((frame * 17 + 83) % 256) }

func render(_ frame: Int) -> any MTLCommandBuffer {
    let base = frame % recordCount * recordSize
    let command = queue.makeCommandBuffer()!
    let pass = MTLRenderPassDescriptor()
    pass.colorAttachments[0].texture = texture
    pass.colorAttachments[0].storeAction = .store
    pass.colorAttachments[0].loadAction = .clear
    pass.colorAttachments[0].clearColor = MTLClearColor(red: 0.1, green: 0.15, blue: 0.2, alpha: 1)
    let encoder = command.makeRenderCommandEncoder(descriptor: pass)!
    encoder.setRenderPipelineState(pipeline)
    encoder.setVertexBuffer(stream, offset: base, index: 0)
    encoder.setVertexBuffer(grid, offset: 0, index: 1)
    var previous = -1
    for draw in 0..<draws {
        let slot = draw * writes / draws
        let offset = base + slot * stride
        if slot != previous {
            let range = stream.contents().advanced(by: offset)
            range.initializeMemory(as: UInt8.self, repeating: 0, count: payloadSizes[slot % 3])
            let points = range.bindMemory(to: SIMD2<Float>.self, capacity: 3)
            let x = (Float(slot % 8) - 3.5) * 0.24
            let y = (Float(slot / 8 % 6) - 2.5) * 0.30
            points[0] = SIMD2(x - 0.08, y - 0.08)
            points[1] = SIMD2(x + 0.08, y - 0.08)
            points[2] = SIMD2(x, y + 0.08)
            previous = slot
        }
        encoder.setVertexBufferOffset(offset, index: 0)
        var color = SIMD4<Float>(227.0 / 255, Float((slot + 40) & 255) / 255, Float(blue(frame)) / 255, 1)
        encoder.setFragmentBytes(&color, length: MemoryLayout<SIMD4<Float>>.size, index: 0)
        let gridRange = drawRanges[draw]
        encoder.drawPrimitives(type: .triangle, vertexStart: gridRange.lowerBound, vertexCount: gridRange.count)
    }
    encoder.endEncoding()
    let blit = command.makeBlitCommandEncoder()!
    blit.copy(from: texture, sourceSlice: 0, sourceLevel: 0,
              sourceOrigin: MTLOrigin(x: 109, y: 672, z: 0),
              sourceSize: MTLSize(width: 1, height: 1, depth: 1),
              to: readback, destinationOffset: frame * markerStride,
              destinationBytesPerRow: markerStride, destinationBytesPerImage: markerStride)
    blit.endEncoding()
    command.commit()
    return command
}

func measure(_ count: Int) {
    readback.contents().initializeMemory(as: UInt8.self, repeating: 0, count: readback.length)
    var completions: [any MTLCommandBuffer] = []
    completions.reserveCapacity(count)
    let start = DispatchTime.now().uptimeNanoseconds
    for i in 0..<count {
        if i >= recordCount {
            completions[i - recordCount].waitUntilCompleted()
        }
        completions.append(autoreleasepool { render(i) })
    }
    completions[count - 1].waitUntilCompleted()
    let end = DispatchTime.now().uptimeNanoseconds
    let bytes = readback.contents().assumingMemoryBound(to: UInt8.self)
    var endTimes: [Double] = []
    for i in 0..<count {
        let mark = bytes.advanced(by: i * markerStride)
        precondition(mark[0] == 227 && mark[2] == blue(i) && mark[3] == 255)
        precondition(completions[i].status == .completed)
        endTimes.append(completions[i].gpuEndTime)
    }
    let gaps = zip(endTimes, endTimes.dropFirst()).map { ($1 - $0) * 1000 }.sorted()
    precondition(gaps.count == count - 1 && gaps[0] > 0)
    let p99 = gaps[Int(ceil(Double(gaps.count) * 0.99)) - 1]
    let mean = Double(end - start) / Double(count) / 1_000_000
    print(String(format: "%d verified outputs %.3f ms/frame, %.3f ms completion p99", count, mean, p99))
}

measure(12)
measure(frames)
measure(frames)
measure(frames)
