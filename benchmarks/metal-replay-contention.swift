import Darwin
import Foundation
import Metal

let device = MTLCreateSystemDefaultDevice()!
let queue = device.makeCommandQueue(maxCommandBufferCount: 1)!
queue.label = "Raid replay independent test queue"
let drawsPerBuffer = Int(CommandLine.arguments.dropFirst().first ?? "8") ?? 0
precondition(drawsPerBuffer > 0 && CommandLine.arguments.count <= 2, "Expected a positive draw count")

let source = """
#include <metal_stdlib>
using namespace metal;

vertex float4 triangle(uint id [[vertex_id]]) {
    float2 positions[] = { float2(-1, -1), float2(3, -1), float2(-1, 3) };
    return float4(positions[id], 0, 1);
}

fragment float4 workload(float4 position [[position]], constant uint& phase [[buffer(0)]]) {
    float3 value = fract(float3(position.xy, position.x + position.y) * 0.001f + float(phase) * 0.0003f);
    for (uint i = 0; i < 12; ++i) {
        value = fract(fma(value.zxy, float3(1.013f, 1.021f, 1.041f), value) + 0.7f);
    }
    return float4(value, 0.48f);
}
"""
let library = try device.makeLibrary(source: source, options: nil)
let pipelineOptions = MTLRenderPipelineDescriptor()
pipelineOptions.vertexFunction = library.makeFunction(name: "triangle")
pipelineOptions.fragmentFunction = library.makeFunction(name: "workload")
let attachment = pipelineOptions.colorAttachments[0]!
attachment.pixelFormat = .rgba8Unorm
attachment.isBlendingEnabled = true
attachment.sourceRGBBlendFactor = .sourceAlpha
attachment.destinationRGBBlendFactor = .oneMinusSourceAlpha
let pipeline = try device.makeRenderPipelineState(descriptor: pipelineOptions)

func texture(_ size: Int) -> any MTLTexture {
    let descriptor = MTLTextureDescriptor.texture2DDescriptor(
        pixelFormat: .rgba8Unorm, width: size, height: size, mipmapped: false)
    descriptor.usage = .renderTarget
    descriptor.storageMode = .private
    return device.makeTexture(descriptor: descriptor)!
}

let target = texture(512)
let marker = texture(16)

func now() -> UInt64 {
    clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
}

func report(_ item: [String: Any]) throws {
    let data = try JSONSerialization.data(withJSONObject: item, options: [.sortedKeys])
    print(String(decoding: data, as: UTF8.self))
    fflush(stdout)
}

func submit(target: any MTLTexture, label: String, seed: UInt32, draws: Int) -> any MTLCommandBuffer {
    let command = queue.makeCommandBuffer()!
    command.label = label
    let pass = MTLRenderPassDescriptor()
    pass.colorAttachments[0].texture = target
    pass.colorAttachments[0].loadAction = .clear
    pass.colorAttachments[0].storeAction = .store
    let encoder = command.makeRenderCommandEncoder(descriptor: pass)!
    encoder.setRenderPipelineState(pipeline)
    for index in 0..<draws {
        var phase = seed &+ UInt32(index)
        encoder.setFragmentBytes(&phase, length: MemoryLayout<UInt32>.size, index: 0)
        encoder.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 3)
    }
    encoder.endEncoding()
    command.commit()
    return command
}

func wait(_ command: any MTLCommandBuffer) -> (UInt64, UInt64) {
    command.waitUntilCompleted()
    precondition(command.status == .completed, "Metal command failed: \(String(describing: command.error))")
    let start = UInt64(command.gpuStartTime * 1_000_000_000)
    let end = UInt64(command.gpuEndTime * 1_000_000_000)
    precondition(end > start, "Metal command has no measured GPU time")
    return (start, end)
}

_ = wait(submit(target: target, label: "Raid contention warmup", seed: 0, draws: drawsPerBuffer))
try report(["event": "ready", "pid": getpid(), "device": device.name, "draws_per_buffer": drawsPerBuffer])
while let line = readLine() {
    let words = line.split(separator: " ")
    guard words.count == 2, let index = Int(words[1]), index >= 0 else {
        fatalError("Expected a command and a nonnegative index")
    }
    if words[0] == "mark" {
        let (start, end) = wait(submit(target: marker, label: "Raid contention marker \(index)",
                                       seed: UInt32(truncatingIfNeeded: index), draws: 1))
        try report(["event": "marker", "index": index, "gpu_start_ns": start, "gpu_end_ns": end])
    } else if words[0] == "pulse" {
        let deadline = now() + 300_000_000
        var windows: [[UInt64]] = []
        repeat {
            let ordinal = windows.count
            let command = submit(target: target, label: "Raid contention \(index) buffer \(ordinal)",
                                 seed: UInt32(truncatingIfNeeded: ordinal * drawsPerBuffer), draws: drawsPerBuffer)
            if ordinal == 0 {
                try report(["event": "armed", "index": index])
            }
            let (start, end) = wait(command)
            windows.append([start, end])
        } while now() < deadline
        try report(["event": "pulse", "index": index, "gpu_buffers_ns": windows])
    } else {
        fatalError("Unrecognized test command")
    }
}
