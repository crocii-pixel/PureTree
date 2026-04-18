#pragma once

#include <vector>
#include <stdexcept>
#include <algorithm>
#include <utility>
#include <type_traits>
#include <cstdint> 
#include <functional> // Required for std::less (Pointer UB safe comparison)

template <typename T>
class CSlotMap {
    static_assert((std::is_nothrow_move_constructible_v<T> && std::is_nothrow_move_assignable_v<T>) || 
                  (std::is_copy_constructible_v<T> && std::is_copy_assignable_v<T>), 
                  "T must be noexcept move constructible AND assignable for optimal Swap-and-Pop performance.");

public:
    using iterator = typename std::vector<T>::iterator;
    using const_iterator = typename std::vector<T>::const_iterator;
    using HandleType = uint64_t;

    CSlotMap() {
        m_indexTable.push_back({0, 0, false, false});
    }
    ~CSlotMap() = default;

    void reserve(size_t capacity) {
        m_data.reserve(capacity);
        m_denseToSparse.reserve(capacity);
        m_indexTable.reserve(capacity + 1); 
    }

    void shrink_to_fit() {
        m_data.shrink_to_fit();
        m_denseToSparse.shrink_to_fit();
        m_indexTable.shrink_to_fit();
        freeIndices_.shrink_to_fit();
    }

    /**
     * @brief Clears the map. 
     * Increments the global generation base to invalidate all existing handles in O(1) conceptually,
     * though we still reset tracking flags for slot reuse.
     */
    void clear() {
        m_data.clear();
        m_denseToSparse.clear();
        freeIndices_.clear();
        
        // Bump the global generation to ensure any handle issued before this clear
        // will never match a handle issued after this clear.
        m_globalGenBase += static_cast<uint32_t>(m_indexTable.size());

        for (uint32_t i = static_cast<uint32_t>(m_indexTable.size()) - 1; i > 0; --i) {
            m_indexTable[i].isActive = false;
            freeIndices_.push_back(i);
            m_indexTable[i].inFreeList = true; 
        }
    }

    HandleType insert(const T& value) { return insert_impl(value); }
    HandleType insert(T&& value) { return insert_impl(std::move(value)); }
    HandleType insert(const T& value, HandleType explicitId) {
        if (explicitId == 0) throw std::invalid_argument("Explicit ID cannot be 0");
        return insert_impl(value, explicitId);
    }
    HandleType insert(T&& value, HandleType explicitId) {
        if (explicitId == 0) throw std::invalid_argument("Explicit ID cannot be 0");
        return insert_impl(std::move(value), explicitId);
    }

    void erase(HandleType id) {
        size_t denseIdx;
        if (!tryGetDenseIndex(id, denseIdx)) return; 

        uint32_t index = unpackIndex(id);
        size_t lastDenseIdx = m_data.size() - 1;

        if (denseIdx != lastDenseIdx) {
            m_data[denseIdx] = std::move(m_data[lastDenseIdx]);
            
            HandleType lastId = m_denseToSparse[lastDenseIdx];
            m_denseToSparse[denseIdx] = lastId;
            m_indexTable[unpackIndex(lastId)].denseIdx = denseIdx;
        }

        m_data.pop_back();
        m_denseToSparse.pop_back();

        // Mark as inactive. The generation in indexTable remains the same until next insert,
        // but isActive = false will block any further access via the old handle.
        m_indexTable[index].isActive = false;
        
        if (!m_indexTable[index].inFreeList) {
            freeIndices_.push_back(index);
            m_indexTable[index].inFreeList = true;
        }
    }

    T* getById(HandleType id) {
        size_t denseIdx;
        if (!tryGetDenseIndex(id, denseIdx)) return nullptr;
        return &m_data[denseIdx];
    }
    const T* getById(HandleType id) const {
        size_t denseIdx;
        if (!tryGetDenseIndex(id, denseIdx)) return nullptr;
        return &m_data[denseIdx];
    }

    T& at(HandleType id) {
        size_t denseIdx;
        if (!tryGetDenseIndex(id, denseIdx)) throw std::out_of_range("Invalid ID");
        return m_data[denseIdx];
    }
    const T& at(HandleType id) const {
        size_t denseIdx;
        if (!tryGetDenseIndex(id, denseIdx)) throw std::out_of_range("Invalid ID");
        return m_data[denseIdx];
    }

    T& operator[](HandleType id) { return at(id); }
    const T& operator[](HandleType id) const { return at(id); }

    bool exists(HandleType id) const {
        size_t dummy;
        return tryGetDenseIndex(id, dummy);
    }
    
    HandleType getId(const T* ptr) const {
        if (m_data.empty()) return 0; 
        if (std::less<const T*>{}(ptr, m_data.data()) || 
            std::greater_equal<const T*>{}(ptr, m_data.data() + m_data.size())) {
            return 0; 
        }
        size_t denseIdx = static_cast<size_t>(ptr - m_data.data());
        return m_denseToSparse[denseIdx];
    }
    
    HandleType getId(const_iterator it) const {
        if (it == end()) return 0; 
        return getId(&*it);
    }

    size_t size() const { return m_data.size(); }
    bool empty() const { return m_data.empty(); }

    iterator begin() { return m_data.begin(); }
    const_iterator begin() const { return m_data.begin(); }
    iterator end() { return m_data.end(); }
    const_iterator end() const { return m_data.end(); }

private:
    struct IndexEntry {
        size_t denseIdx = 0;
        uint32_t gen = 1;
        bool isActive = false;
        bool inFreeList = false;
    };

    std::vector<T> m_data;                 
    std::vector<HandleType> m_denseToSparse;   
    std::vector<IndexEntry> m_indexTable;
    std::vector<uint32_t> freeIndices_;
    
    // Global generation counter to issue unique IDs across the map's lifetime.
    uint32_t m_globalGenBase = 1;

    static constexpr HandleType packId(uint32_t index, uint32_t gen) {
        return (static_cast<HandleType>(gen) << 32) | static_cast<HandleType>(index);
    }
    static constexpr uint32_t unpackIndex(HandleType id) {
        return static_cast<uint32_t>(id & 0xFFFFFFFF);
    }
    static constexpr uint32_t unpackGen(HandleType id) {
        return static_cast<uint32_t>(id >> 32);
    }

    bool tryGetDenseIndex(HandleType id, size_t& outDenseIdx) const {
        uint32_t index = unpackIndex(id);
        uint32_t gen = unpackGen(id);
        if (index == 0 || index >= m_indexTable.size()) return false;
        
        const auto& entry = m_indexTable[index];
        // Generation must match exactly and the slot must be active.
        if (!entry.isActive || entry.gen != gen) return false;

        outDenseIdx = entry.denseIdx;
        return true;
    }

    template <typename U>
    HandleType insert_impl(U&& value, HandleType explicitId = 0) {
        uint32_t index = 0;
        uint32_t gen;

        if (explicitId != 0) {
            index = unpackIndex(explicitId);
            gen = unpackGen(explicitId);
            
            // [방어 코드 추가] Index 0은 시스템 예약용(Null Handle)이므로 덮어쓰기를 원천 차단합니다.
            if (index == 0) {
                throw std::invalid_argument("Explicit ID cannot use reserved index 0.");
            }
            
            if (index >= m_indexTable.size()) {
                size_t oldSize = m_indexTable.size();
                m_indexTable.resize(index + 1, {0, 1, false, false});
                
                for (size_t i = index; i > oldSize; --i) {
                    freeIndices_.push_back(static_cast<uint32_t>(i - 1));
                    m_indexTable[i - 1].inFreeList = true;
                }
            }
            if (m_indexTable[index].isActive) throw std::runtime_error("Duplicate explicit ID.");
            
            m_indexTable[index].gen = gen;
            // Update global base to avoid future collisions if explicit gen was high.
            if (gen >= m_globalGenBase) m_globalGenBase = gen + 1;
        } else {
            while (!freeIndices_.empty()) {
                uint32_t candidate = freeIndices_.back();
                freeIndices_.pop_back();
                m_indexTable[candidate].inFreeList = false; 
                
                if (!m_indexTable[candidate].isActive) {
                    index = candidate;
                    break;
                }
            }

            if (index == 0) {
                index = static_cast<uint32_t>(m_indexTable.size());
                m_indexTable.push_back({0, 1, false, false}); 
            }
            
            // Issue a globally unique generation number for this new insertion.
            gen = m_globalGenBase++;
            m_indexTable[index].gen = gen;
        }

        size_t denseIdx = m_data.size();
        HandleType newId = packId(index, gen);

        m_data.emplace_back(std::forward<U>(value));
        try {
            m_denseToSparse.push_back(newId);
        } catch (...) {
            m_data.pop_back();
            if (!m_indexTable[index].inFreeList) {
                freeIndices_.push_back(index); 
                m_indexTable[index].inFreeList = true;
            }
            throw;
        }

        m_indexTable[index].denseIdx = denseIdx;
        m_indexTable[index].isActive = true;

        return newId;
    }
};