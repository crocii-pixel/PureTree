#pragma once

#include <string>
#include <vector>
#include <unordered_map>
#include <algorithm>
#include <stdexcept>
#include <functional> 
#include "CSlotMap.h"
#include "AnyValue.h"

namespace nsTree {

using hTree = uint64_t;

struct Metrics {
    float force = 1.0f;
    float distance = 1.0f;
    float polarity = 1.0f;
};

struct Node {
    int type = 0;             
    std::string key;          
    AnyValue value;           

    std::vector<hTree> outs;  
    std::vector<hTree> ins;   
    
    uint32_t visitEpoch = 0; 
};

struct Link {
    hTree from = 0;       
    hTree to = 0;         
    int type = 0;         
    Metrics metrics;      
};

struct TreeConfig {
    bool enforceUniqueKeys = true; 
    bool allowEmptyKeys = false;   
};

/**
 * @enum FilterMode
 * @brief Traversal filtering mode.
 */
enum class FilterMode {
    None,   // No filtering, traverse all links
    Skip,   // Skip links matching the provided types
    Only    // Traverse exclusively the links matching the provided types
};

class CPureTree {
public:
    using OnNodeValueChanged = std::function<void(hTree nodeId, const AnyValue& newValue)>;
    OnNodeValueChanged onValueChanged = nullptr;

    explicit CPureTree(const TreeConfig& config = TreeConfig()) : config_(config) {
        nodes_.reserve(1024);
        links_.reserve(2048);
    }

    virtual ~CPureTree() = default;

    CPureTree(const CPureTree&) = delete;
    CPureTree& operator=(const CPureTree&) = delete;

    void shrink_to_fit() {
        nodes_.shrink_to_fit();
        links_.shrink_to_fit();
    }

    // ==========================================
    // 1. Entity & Link Creation
    // ==========================================

    virtual hTree addNode(int type, const std::string& key, AnyValue val = AnyValue()) {
        if (key.empty()) {
            if (!config_.allowEmptyKeys) {
                return 0; 
            }
        } else {
            if (config_.enforceUniqueKeys) {
                if (keyIndex_.find(key) != keyIndex_.end()) {
                    return 0; 
                }
            }
        }

        Node node;
        node.type = type;
        node.key = key;
        node.value = std::move(val);
        
        hTree h = nodes_.insert(std::move(node));
        
        if (!key.empty()) {
            try {
                keyIndex_.insert({key, h}); 
            } catch (...) {
                nodes_.erase(h);
                throw;
            }
        }
        return h;
    }

    virtual hTree addLink(hTree from, hTree to, int type = 0, Metrics metrics = Metrics()) {
        if (!nodes_.exists(from) || !nodes_.exists(to)) return 0;

        Link link;
        link.from = from;
        link.to = to;
        link.type = type;
        link.metrics = metrics;

        hTree h = links_.insert(std::move(link));

        try {
            nodes_[from].outs.push_back(h);
            nodes_[to].ins.push_back(h);
        } catch (...) {
            auto& outs = nodes_[from].outs;
            if (!outs.empty() && outs.back() == h) {
                outs.pop_back();
            }
            links_.erase(h);
            throw;
        }

        return h;
    }

    // ==========================================
    // 2. Safe Deletion & Graph Integrity
    // ==========================================

    virtual void onNodeRemoving(hTree /*nodeId*/) {}

    virtual void removeLink(hTree linkId) {
        Link* link = links_.getById(linkId);
        if (!link) return;

        if (Node* fromNode = nodes_.getById(link->from)) {
            auto& outs = fromNode->outs;
            outs.erase(std::remove(outs.begin(), outs.end(), linkId), outs.end());
        }

        if (Node* toNode = nodes_.getById(link->to)) {
            auto& ins = toNode->ins;
            ins.erase(std::remove(ins.begin(), ins.end(), linkId), ins.end());
        }

        links_.erase(linkId);
    }

    virtual void removeNode(hTree nodeId) {
        Node* node = nodes_.getById(nodeId);
        if (!node) return;

        onNodeRemoving(nodeId);

        std::vector<hTree> linksToDelete;
        linksToDelete.reserve(node->outs.size() + node->ins.size());
        linksToDelete.insert(linksToDelete.end(), node->outs.begin(), node->outs.end());
        linksToDelete.insert(linksToDelete.end(), node->ins.begin(), node->ins.end());

        std::sort(linksToDelete.begin(), linksToDelete.end());
        linksToDelete.erase(std::unique(linksToDelete.begin(), linksToDelete.end()), linksToDelete.end());

        node->outs.clear();
        node->ins.clear();

        for (hTree h : linksToDelete) {
            removeLink(h); 
        }

        if (!node->key.empty()) {
            auto range = keyIndex_.equal_range(node->key);
            for (auto it = range.first; it != range.second; ++it) {
                if (it->second == nodeId) {
                    keyIndex_.erase(it);
                    break;
                }
            }
        }
        nodes_.erase(nodeId);
    }

    // ==========================================
    // 3. Search & Modification
    // ==========================================

    Node* getNode(hTree h) { return nodes_.getById(h); }
    const Node* getNode(hTree h) const { return nodes_.getById(h); }
    Link* getLink(hTree h) { return links_.getById(h); }
    const Link* getLink(hTree h) const { return links_.getById(h); }

    hTree findNode(const std::string& key) const {
        if (key.empty()) return 0;
        
        auto range = keyIndex_.equal_range(key);
        if (range.first == range.second) return 0;

        hTree latest = 0;
        for (auto it = range.first; it != range.second; ++it) {
            if (it->second > latest) latest = it->second;
        }
        return latest;
    }

    std::vector<hTree> findNodes(const std::string& key) const {
        std::vector<hTree> result;
        if (key.empty()) return result;
        
        auto range = keyIndex_.equal_range(key);
        result.reserve(static_cast<std::size_t>(std::distance(range.first, range.second)));
        
        for (auto it = range.first; it != range.second; ++it) {
            result.push_back(it->second);
        }
        
        std::sort(result.begin(), result.end(), std::greater<hTree>());
        return result;
    }

    virtual void setNodeValue(hTree nodeId, AnyValue newVal) {
        Node* node = nodes_.getById(nodeId);
        if (!node) return;
        node->value = std::move(newVal);
        if (onValueChanged) onValueChanged(nodeId, node->value);
    }

    // ==========================================
    // 4. View & Linearization
    // ==========================================

    void linearize(hTree root, std::vector<hTree>& outResult, bool postOrder = true, FilterMode filterMode = FilterMode::None, const std::vector<int>& filterTypes = {}) {
        outResult.clear(); 
        if (!nodes_.exists(root)) return;

        // Smart Edge-case Handling
        if (filterTypes.empty()) {
            if (filterMode == FilterMode::Skip) {
                filterMode = FilterMode::None; // Skip nothing -> None
            } else if (filterMode == FilterMode::Only) {
                // If "Only" is specified but no types are provided, it means NO links can be traversed.
                outResult.push_back(root);
                return;
            }
        }

        if (++m_currentTraversalEpoch == 0) { 
            m_currentTraversalEpoch = 1;
            for (auto it = nodes_.begin(); it != nodes_.end(); ++it) {
                it->visitEpoch = 0;
            }
        }
        traverse(root, outResult, postOrder, filterMode, filterTypes);
    }

    std::vector<hTree> linearize(hTree root, bool postOrder = true, FilterMode filterMode = FilterMode::None, const std::vector<int>& filterTypes = {}) {
        std::vector<hTree> result;
        linearize(root, result, postOrder, filterMode, filterTypes);
        return result; 
    }

    void clear() {
        nodes_.clear();
        links_.clear();
        keyIndex_.clear();
        m_currentTraversalEpoch = 0;
    }

protected:
    const TreeConfig config_; 

    CSlotMap<Node> nodes_;
    CSlotMap<Link> links_;
    
    std::unordered_multimap<std::string, hTree> keyIndex_;
    
    uint32_t m_currentTraversalEpoch = 0; 

private:
    void traverse(hTree current, std::vector<hTree>& result, bool postOrder, FilterMode filterMode, const std::vector<int>& filterTypes) {
        Node* node = nodes_.getById(current);
        if (!node) return;

        if (node->visitEpoch == m_currentTraversalEpoch) return;
        node->visitEpoch = m_currentTraversalEpoch;

        if (!postOrder) result.push_back(current);

        for (hTree linkId : node->outs) {
            Link* link = links_.getById(linkId);
            if (!link) continue;
            
            if (filterMode == FilterMode::Skip) {
                if (std::find(filterTypes.begin(), filterTypes.end(), link->type) != filterTypes.end()) continue;
            } else if (filterMode == FilterMode::Only) {
                if (std::find(filterTypes.begin(), filterTypes.end(), link->type) == filterTypes.end()) continue;
            }

            traverse(link->to, result, postOrder, filterMode, filterTypes);
        }

        if (postOrder) result.push_back(current);
    }
};

} // namespace nsTree