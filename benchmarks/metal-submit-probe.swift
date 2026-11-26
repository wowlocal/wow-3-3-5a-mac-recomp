import Foundation
import Metal
import simd

let width = 1365
let height = 768
let batchCount = 48
let readbackStride = 256
let measurementFrames = 200

func require(_ condition: Bool, _ message: String) {
    guard condition else { fatalError(message) }
}

let device = MTLCreateSystemDefaultDevice()!
let queue = device.makeCommandQueue(maxCommandBufferCount: 96)!
let shader = """
#include <metal_stdlib>
using namespace metal;
vertex float4 vertex_position(const device float2* points [[buffer(0)]], uint id [[vertex_id]]) {
    return float4(points[id], 0.0, 1.0);
}
fragment float4 fragment_color(constant float4& color [[buffer(0)]]) {
    return color;
}
"""
let library = try device.makeLibrary(source: shader, options: nil)
let pipelineDescriptor = MTLRenderPipelineDescriptor()
pipelineDescriptor.vertexFunction = library.makeFunction(name: "vertex_position")
pipelineDescriptor.fragmentFunction = library.makeFunction(name: "fragment_color")
pipelineDescriptor.colorAttachments[0].pixelFormat = .rgba8Unorm
let pipeline = try device.makeRenderPipelineState(descriptor: pipelineDescriptor)

let descriptor = MTLTextureDescriptor.texture2DDescriptor(
    pixelFormat: .rgba8Unorm, width: width, height: height, mipmapped: false)
descriptor.usage = .renderTarget
descriptor.storageMode = .shared
let target = device.makeTexture(descriptor: descriptor)!
let readback = device.makeBuffer(length: measurementFrames * readbackStride, options: .storageModeShared)!

func frameColor(_ frame: Int) -> UInt8 { UInt8((frame * 17 + 83) % 256) }

let vertexStride = MemoryLayout<SIMD2<Float>>.stride
let buffer = device.makeBuffer(length: batchCount * 3 * vertexStride, options: .storageModeShared)!
let vertices = buffer.contents().bindMemory(to: SIMD2<Float>.self, capacity: batchCount * 3)
for batch in 0..<batchCount {
    let center = (Float(batch % 8) - 3.5) * 0.24
    let height = (Float(batch / 8) - 2.5) * 0.30
    vertices[batch * 3] = SIMD2(center - 0.08, height - 0.08)
    vertices[batch * 3 + 1] = SIMD2(center + 0.08, height - 0.08)
    vertices[batch * 3 + 2] = SIMD2(center, height + 0.08)
}

func render(split: Bool, frame: Int = 0, copyMarker: Bool = false) -> any MTLCommandBuffer {
    let batchSize = split ? 1 : batchCount
    var last: (any MTLCommandBuffer)?
    for first in stride(from: 0, to: batchCount, by: batchSize) {
        let command = queue.makeCommandBuffer()!
        let pass = MTLRenderPassDescriptor()
        let colorAttachment = pass.colorAttachments[0]!
        colorAttachment.texture = target
        colorAttachment.storeAction = .store
        colorAttachment.loadAction = first == 0 ? .clear : .load
        colorAttachment.clearColor = MTLClearColor(red: 0.1, green: 0.15, blue: 0.2, alpha: 1)
        let encoder = command.makeRenderCommandEncoder(descriptor: pass)!
        encoder.setRenderPipelineState(pipeline)
        encoder.setVertexBuffer(buffer, offset: 0, index: 0)
        for batch in first..<(first + batchSize) {
            var color = SIMD4<Float>(227.0 / 255, Float(batch + 40) / 255, Float(frameColor(frame)) / 255, 1)
            encoder.setFragmentBytes(&color, length: MemoryLayout<SIMD4<Float>>.size, index: 0)
            encoder.drawPrimitives(type: .triangle, vertexStart: batch * 3, vertexCount: 3)
        }
        encoder.endEncoding()
        if copyMarker && first + batchSize == batchCount {
            let blit = command.makeBlitCommandEncoder()!
            blit.copy(from: target, sourceSlice: 0, sourceLevel: 0,
                      sourceOrigin: MTLOrigin(x: 109, y: 672, z: 0),
                      sourceSize: MTLSize(width: 1, height: 1, depth: 1),
                      to: readback, destinationOffset: frame * readbackStride,
                      destinationBytesPerRow: readbackStride, destinationBytesPerImage: readbackStride)
            blit.endEncoding()
        }
        command.commit()
        last = command
    }
    return last!
}

func readPixels(split: Bool) -> [UInt8] {
    let command = render(split: split)
    command.waitUntilCompleted()
    require(command.status == .completed, "Metal failed to draw the test frame")
    var pixels = [UInt8](repeating: 0, count: width * height * 4)
    pixels.withUnsafeMutableBytes { base in
        target.getBytes(base.baseAddress!, bytesPerRow: width * 4,
                        from: MTLRegionMake2D(0, 0, width, height), mipmapLevel: 0)
    }
    return pixels
}

let combined = readPixels(split: false)
let split = readPixels(split: true)
let colored = stride(from: 0, to: split.count, by: 4).filter { split[$0] == 227 }.count
require(combined == split && colored > 10_000, "Metal outputs differed")
print("Identical rendered output (\(colored) colored pixels)")

func measure(split: Bool, frames: Int) {
    readback.contents().initializeMemory(as: UInt8.self, repeating: 0, count: readback.length)
    let start = DispatchTime.now().uptimeNanoseconds
    var last: (any MTLCommandBuffer)?
    for frame in 0..<frames {
        last = autoreleasepool { render(split: split, frame: frame, copyMarker: true) }
    }
    last!.waitUntilCompleted()
    require(last!.status == .completed, "Metal failed during measurement")
    let elapsed = Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000
    let markers = readback.contents().bindMemory(to: UInt8.self, capacity: readback.length)
    for frame in 0..<frames {
        let pixel = markers.advanced(by: frame * readbackStride)
        require(pixel[0] == 227 && pixel[1] == 40 && pixel[2] == frameColor(frame) && pixel[3] == 255,
                "The rendered marker was missing or stale on frame \(frame)")
    }
    let label = split ? "split" : "combined"
    print("\(label) \(frames) frames \(String(format: "%.3f", elapsed / Double(frames))) ms/frame")
}

measure(split: false, frames: 30)
measure(split: true, frames: 30)
for sample in 0..<3 {
    measure(split: sample.isMultiple(of: 2), frames: measurementFrames)
    measure(split: !sample.isMultiple(of: 2), frames: measurementFrames)
}
